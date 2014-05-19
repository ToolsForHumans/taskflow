# -*- coding: utf-8 -*-

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from taskflow.engines.action_engine import executor as ex
from taskflow import exceptions as excp
from taskflow import retry as r
from taskflow import states as st
from taskflow import task
from taskflow.utils import misc


_WAITING_TIMEOUT = 60  # in seconds


class FutureGraphAction(object):
    """Graph action build around futures returned by task action.

    This graph action schedules all task it can for execution and than
    waits on returned futures. If task executor is able to execute tasks
    in parallel, this enables parallel flow run and reversion.
    """

    # Informational states this action yields while running, not useful to
    # have the engine record but useful to provide to end-users when doing
    # execution iterations.
    ignorable_states = (st.SCHEDULING, st.WAITING, st.RESUMING, st.ANALYZING)

    def __init__(self, analyzer, storage, task_action, retry_action):
        self._analyzer = analyzer
        self._storage = storage
        self._task_action = task_action
        self._retry_action = retry_action

    def is_running(self):
        return self._storage.get_flow_state() == st.RUNNING

    def _schedule_node(self, node):
        """Schedule a single node for execution."""
        if isinstance(node, task.BaseTask):
            return self._schedule_task(node)
        elif isinstance(node, r.Retry):
            return self._schedule_retry(node)
        else:
            raise TypeError("Unknown how to schedule node %s" % node)

    def _schedule(self, nodes):
        """Schedule a group of nodes for execution."""
        futures = set()
        for node in nodes:
            try:
                futures.add(self._schedule_node(node))
            except Exception:
                # Immediately stop scheduling future work so that we can
                # exit execution early (rather than later) if a single task
                # fails to schedule correctly.
                return (futures, [misc.Failure()])
        return (futures, [])

    def execute_iter(self, timeout=None):
        if timeout is None:
            timeout = _WAITING_TIMEOUT

        # Prepare flow to be resumed
        yield st.RESUMING
        next_nodes = self._prepare_flow_for_resume()
        next_nodes.update(self._analyzer.get_next_nodes())

        # Schedule nodes to be worked on
        yield st.SCHEDULING
        if self.is_running():
            not_done, failures = self._schedule(next_nodes)
        else:
            not_done, failures = (set(), [])

        # Run!
        #
        # At this point we need to ensure we wait for all active nodes to
        # finish running (even if we are asked to suspend) since we can not
        # preempt those tasks (maybe in the future we will be better able to do
        # this).
        while not_done:
            yield st.WAITING

            # TODO(harlowja): maybe we should start doing 'yield from' this
            # call sometime in the future, or equivalent that will work in
            # py2 and py3.
            done, not_done = self._task_action.wait_for_any(not_done, timeout)

            # Analyze the results and schedule more nodes (unless we had
            # failures). If failures occurred just continue processing what
            # is running (so that we don't leave it abandoned) but do not
            # schedule anything new.
            yield st.ANALYZING
            next_nodes = set()
            for future in done:
                try:
                    node, event, result = future.result()
                    if isinstance(node, task.BaseTask):
                        self._complete_task(node, event, result)
                    if isinstance(result, misc.Failure):
                        if event == ex.EXECUTED:
                            self._process_atom_failure(node, result)
                        else:
                            failures.append(result)
                except Exception:
                    failures.append(misc.Failure())
                else:
                    try:
                        more_nodes = self._analyzer.get_next_nodes(node)
                    except Exception:
                        failures.append(misc.Failure())
                    else:
                        next_nodes.update(more_nodes)
            if next_nodes and not failures and self.is_running():
                yield st.SCHEDULING
                # Recheck incase someone suspended it.
                if self.is_running():
                    more_not_done, failures = self._schedule(next_nodes)
                    not_done.update(more_not_done)

        if failures:
            misc.Failure.reraise_if_any(failures)
        if self._analyzer.get_next_nodes():
            yield st.SUSPENDED
        elif self._analyzer.is_success():
            yield st.SUCCESS
        else:
            yield st.REVERTED

    def _schedule_task(self, task):
        """Schedules the given task for revert or execute depending
        on its intention.
        """
        intention = self._storage.get_atom_intention(task.name)
        if intention == st.EXECUTE:
            return self._task_action.schedule_execution(task)
        elif intention == st.REVERT:
            return self._task_action.schedule_reversion(task)
        else:
            raise excp.ExecutionFailure("Unknown how to schedule task with"
                                        " intention: %s" % intention)

    def _complete_task(self, task, event, result):
        """Completes the given task, process task failure."""
        if event == ex.EXECUTED:
            self._task_action.complete_execution(task, result)
        else:
            self._task_action.complete_reversion(task, result)

    def _schedule_retry(self, retry):
        """Schedules the given retry for revert or execute depending
        on its intention.
        """
        intention = self._storage.get_atom_intention(retry.name)
        if intention == st.EXECUTE:
            return self._retry_action.execute(retry)
        elif intention == st.REVERT:
            return self._retry_action.revert(retry)
        elif intention == st.RETRY:
            self._retry_action.change_state(retry, st.RETRYING)
            self._retry_subflow(retry)
            return self._retry_action.execute(retry)
        else:
            raise excp.ExecutionFailure("Unknown how to schedule retry with"
                                        " intention: %s" % intention)

    def _process_atom_failure(self, atom, failure):
        """On atom failure find its retry controller, ask for the action to
        perform with failed subflow and set proper intention for subflow nodes.
        """
        retry = self._analyzer.find_atom_retry(atom)
        if retry:
            # Ask retry controller what to do in case of failure
            action = self._retry_action.on_failure(retry, atom, failure)
            if action == r.RETRY:
                # Prepare subflow for revert
                self._storage.set_atom_intention(retry.name, st.RETRY)
                for node in self._analyzer.iterate_subgraph(retry):
                    self._storage.set_atom_intention(node.name, st.REVERT)
            elif action == r.REVERT:
                # Ask parent checkpoint
                self._process_atom_failure(retry, failure)
            elif action == r.REVERT_ALL:
                # Prepare all flow for revert
                self._revert_all()
        else:
            self._revert_all()

    def _revert_all(self):
        for node in self._analyzer.iterate_all_nodes():
            self._storage.set_atom_intention(node.name, st.REVERT)

    def _prepare_flow_for_resume(self):
        for node in self._analyzer.iterate_all_nodes():
            if self._analyzer.get_state(node) == st.FAILURE:
                self._process_atom_failure(node, self._storage.get(node.name))
        for retry in self._analyzer.iterate_retries(st.RETRYING):
            self._retry_subflow(retry)
        next_nodes = set()
        for node in self._analyzer.iterate_all_nodes():
            if self._analyzer.get_state(node) in (st.RUNNING, st.REVERTING):
                next_nodes.add(node)
        return next_nodes

    def _reset_nodes(self, nodes_iter, intention=st.EXECUTE):
        for node in nodes_iter:
            if isinstance(node, task.BaseTask):
                self._task_action.change_state(node, st.PENDING, progress=0.0)
            elif isinstance(node, r.Retry):
                self._retry_action.change_state(node, st.PENDING)
            else:
                raise TypeError("Unknown how to reset node %s" % node)
            self._storage.set_atom_intention(node.name, intention)

    def reset_all(self):
        self._reset_nodes(self._analyzer.iterate_all_nodes())

    def _retry_subflow(self, retry):
        self._storage.set_atom_intention(retry.name, st.EXECUTE)
        self._reset_nodes(self._analyzer.iterate_subgraph(retry))

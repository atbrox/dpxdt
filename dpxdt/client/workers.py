#!/usr/bin/env python
# Copyright 2013 Brett Slatkin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Workers for driving screen captures, perceptual diffs, and related work."""

import Queue
import logging
import sys
import threading

# Local Libraries
import gflags
FLAGS = gflags.FLAGS


gflags.DEFINE_float(
    'polltime', 1.0,
    'How long to sleep between polling for work from an input queue, '
    'a subprocess, or a waiting timer.')


class WorkItem(object):
    """Base work item that can be handled by a worker thread."""

    # Set this to True for WorkItems that should never wait for their
    # return values.
    fire_and_forget = False

    def __init__(self):
        self.error = None
        self.done = False

    @staticmethod
    def _print_tree(obj):
        if isinstance(obj, dict):
            result = []
            for key, value in obj.iteritems():
                result.append("%s: %s" % (key, WorkItem._print_tree(value)))
            return '{%s}' % ', '.join(result)
        else:
            value_str = repr(obj)
            if len(value_str) > 100:
                return '%s...%s' % (value_str[:100], value_str[-1])
            else:
                return value_str

    def _get_dict_for_repr(self):
        return self.__dict__

    def __repr__(self):
        return '%s.%s(%s)' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self._print_tree(self._get_dict_for_repr()))

    def check_result(self):
        # TODO: For WorkflowItems, remove generator.throw(*item.error) from
        # the stack trace since it's noise. General approach outlined here:
        # https://github.com/mitsuhiko/jinja2/blob/master/jinja2/debug.py
        if self.error:
            raise self.error[0], self.error[1], self.error[2]


class WorkerThread(threading.Thread):
    """Base worker thread that handles items one at a time."""

    def __init__(self, input_queue, output_queue):
        """Initializer.

        Args:
            input_queue: Queue this worker consumes work from.
            output_queue: Queue where this worker puts new work items, if any.
        """
        threading.Thread.__init__(self)
        self.daemon = True
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.interrupted = False
        self.polltime = FLAGS.polltime

    def stop(self):
        """Stops the thread but does not join it."""
        if self.interrupted:
            return
        self.interrupted = True

    def run(self):
        while not self.interrupted:
            try:
                item = self.input_queue.get(True, self.polltime)
            except Queue.Empty:
                self.handle_nothing()
                continue

            try:
                next_item = self.handle_item(item)
            except Exception, e:
                item.error = sys.exc_info()
                logging.exception('%s error item=%r', self.worker_name, item)
                self.output_queue.put(item)
            else:
                logging.debug('%s processed item=%r', self.worker_name, item)
                if not isinstance(item, WorkflowItem):
                    item.done = True
                if next_item:
                    self.output_queue.put(next_item)
            finally:
                self.input_queue.task_done()

    @property
    def worker_name(self):
        return '%s:%s' % (self.__class__.__name__, self.ident)

    def handle_nothing(self):
        """Runs whenever there are no items in the queue."""
        pass

    def handle_item(self, item):
        """Handles a single item.

        Args:
            item: WorkItem to process.

        Returns:
            A WorkItem that should go on the output queue. If None, then
            the provided work item is considered finished and no
            additional work is needed.
        """
        raise NotImplemented


class WorkflowItem(WorkItem):
    """Work item for coordinating other work items.

    To use: Sub-class and override run(). Yield WorkItems you want processed
    as part of this workflow. Exceptions in child workflows will be reinjected
    into the run() generator at the yield point. Results will be available on
    the WorkItems returned by yield statements. Yield a list of WorkItems
    to do them in parallel. The first error encountered for the whole list
    will be raised if there's an exception.
    """

    def __init__(self, *args, **kwargs):
        WorkItem.__init__(self)
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self.root = False

    def run(self, *args, **kwargs):
        yield 'Yo dawg'


class WaitAny(object):
    """Return control to a workflow after any one of these items finishes.

    As soon as a single work item completes, the combined barrier will be
    fulfilled and control will return to the WorkflowItem. The return values
    will be WorkItem instances, the same ones passed into WaitAny. For
    WorkflowItems the return values will be WorkflowItems if the work is not
    finished yet, and the final return value once work is finished.
    """

    def __init__(self, items):
        """Initializer.

        Args:
            items: List of WorkItems to wait for.
        """
        self.items = items


class Barrier(list):
    """Barrier for running multiple WorkItems in parallel."""

    def __init__(self, workflow, generator, work):
        """Initializer.

        Args:
            workflow: WorkflowItem instance this is for.
            generator: Current state of the WorkflowItem's generator.
            work: Next set of work to do. May be a single WorkItem object or
                a list or tuple that contains a set of WorkItems to run in
                parallel.
        """
        list.__init__(self)
        self.workflow = workflow
        self.generator = generator

        if isinstance(work, (list, tuple)):
            self[:] = list(work)
            self.was_list = True
            self.wait_any = False
        elif isinstance(work, WaitAny):
            self[:] = list(work.items)
            self.was_list = True
            self.wait_any = True
        else:
            self[:] = [work]
            self.was_list = False
            self.wait_any = False

        for item in self:
            assert isinstance(item, WorkItem)

        self.error = None

    @property
    def outstanding(self):
        """Returns whether or not this barrier has pending work."""
        # Allow the same WorkItem to be yielded multiple times but not
        # count towards blocking the barrier.
        done_count = len([w for w in self if w.done or w.fire_and_forget])

        if self.wait_any and done_count > 0:
            return False

        if done_count == len(self):
            return False

        return True

    def get_item(self):
        """Returns the item to send back into the workflow generator."""
        if self.was_list:
            blocking_items = self[:]
            self[:] = []
            for item in blocking_items:
                if isinstance(item, WorkflowItem) and item.done:
                    self.append(item.result)
                else:
                    self.append(item)
            return self
        else:
            return self[0]

    def finish(self, item):
        """Marks the given item that is part of the barrier as done."""
        # Copy the error from any failed item to be the error for the whole
        # barrier. The last error seen "wins"
        if item.error and not self.error:
            self.error = item.error


class Return(Exception):
    """Raised in WorkflowItem.run to return a result to the caller."""

    def __init__(self, result=None):
        """Initializer.

        Args:
            result: Result of a WorkflowItem, if any.
        """
        self.result = result


class WorkflowThread(WorkerThread):
    """Worker thread for running workflows."""

    def __init__(self, input_queue, output_queue):
        """Initializer.

        Args:
            input_queue: Queue this worker consumes work from. These should be
                WorkflowItems to process, or any WorkItems registered with this
                class using the register() method.
            output_queue: Queue where this worker puts finished work items,
                if any.
        """
        WorkerThread.__init__(self, input_queue, output_queue)
        self.pending = {}
        self.work_map = {}
        self.worker_threads = []
        self.register(WorkflowItem, input_queue)

    # TODO: Implement drain, to let all existing work finish but no new work
    # allowed at the top of the funnel.

    def start(self):
        """Starts the coordinator thread and all related worker threads."""
        assert not self.interrupted
        for thread in self.worker_threads:
            thread.start()
        WorkerThread.start(self)

    def stop(self):
        """Stops the coordinator thread and all related threads."""
        if self.interrupted:
            return
        for thread in self.worker_threads:
            thread.interrupted = True
        self.interrupted = True

    def join(self):
        """Joins the coordinator thread and all worker threads."""
        for thread in self.worker_threads:
            thread.join()
        WorkerThread.join(self)

    def wait_until_interrupted(self):
        """Waits until this worker is interrupted by a terminating signal."""
        while True:
            try:
                item = self.output_queue.get(True, 1)
            except Queue.Empty:
                continue
            except KeyboardInterrupt:
                logging.debug('Exiting')
                return
            else:
                item.check_result()
                return

    def register(self, work_type, queue):
        """Registers where work for a specific type can be executed.

        Args:
            work_type: Sub-class of WorkItem to register.
            queue: Queue instance where WorkItems of the work_type should be
                enqueued when they are yielded by WorkflowItems being run by
                this worker.
        """
        self.work_map[work_type] = queue

    def enqueue_barrier(self, barrier):
        for item in barrier:
            if item.done:
                # Don't reenqueue items that are already done.
                continue
            if isinstance(item, WorkflowItem):
                target_queue = self.input_queue
            else:
                target_queue = self.work_map[type(item)]

            if not item.fire_and_forget:
                self.pending[item] = barrier
            target_queue.put(item)

    def dequeue_barrier(self, item):
        # This is a WorkItem from a worker thread that has finished and
        # needs to be reinjected into a WorkflowItem generator.
        barrier = self.pending.pop(item, None)
        if not barrier:
            # Item was already finished in another barrier, or was
            # fire-and-forget and never part of a barrier; ignore it.
            return None

        barrier.finish(item)
        if barrier.outstanding and not barrier.error:
            # More work to do and no error seen. Keep waiting.
            return None

        for work in barrier:
            # The barrier has been fulfilled one way or another. Clear out any
            # other pending parts of the barrier so they don't trigger again.
            self.pending.pop(work, None)

        return barrier

    def handle_item(self, item):
        if isinstance(item, WorkflowItem) and not item.done:
            workflow = item
            try:
                generator = item.run(*item.args, **item.kwargs)
            except TypeError, e:
                raise TypeError('%s: item=%r', e, item)
            item = None
        else:
            barrier = self.dequeue_barrier(item)
            if not barrier:
                return
            item = barrier.get_item()
            workflow = barrier.workflow
            generator = barrier.generator

        while True:
            logging.debug('Transitioning workflow=%r, generator=%r, item=%r',
                          workflow, generator, item)
            try:
                try:
                    if item is not None and item.error:
                        next_item = generator.throw(*item.error)
                    elif isinstance(item, WorkflowItem):
                        next_item = generator.send(item.result)
                    else:
                        next_item = generator.send(item)
                except StopIteration:
                    workflow.done = True
                except Return, e:
                    workflow.done = True
                    workflow.result = e.result
                except Exception, e:
                    workflow.done = True
                    workflow.error = sys.exc_info()
            finally:
                if workflow.done:
                    if workflow.root:
                        # Root workflow finished. This goes to the output
                        # queue so it can be received by the main thread.
                        return workflow
                    else:
                        # Sub-workflow finished. Reinject it into the
                        # workflow so a pending parent can catch it.
                        self.input_queue.put(workflow)
                        return

            barrier = Barrier(workflow, generator, next_item)
            self.enqueue_barrier(barrier)
            if barrier.outstanding:
                break

            # If a returned barrier has no oustanding parts, immediately
            # progress the workflow.
            item = barrier.get_item()


def get_coordinator():
    """Creates a coordinator and returns it."""
    workflow_queue = Queue.Queue()
    complete_queue = Queue.Queue()
    coordinator = WorkflowThread(workflow_queue, complete_queue)
    return coordinator

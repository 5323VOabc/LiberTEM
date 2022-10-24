import threading
from typing import TYPE_CHECKING, Iterable, Dict, Callable, List, Optional, Tuple, Any
import time
from libertem.common.executor import WorkerQueueEmpty


if TYPE_CHECKING:
    import numpy as np
    from libertem.common.executor import WorkerQueue, TaskCommHandler
    from libertem.udf.base import UDFTask
    from libertem.io.dataset.base.tiling import DataTile
    from libertem.io.dataset.base.partition import Partition


class CommsDispatcher:
    """
    Monitors a :code:`WorkerQueue` in a background thread
    and launches callbacks in response to messages recieved
    Callbacks are registered as a dictionary of subscriptions
        {topic_name: [callback, ...]]}
    and are called in order they were recieved in the same thread
    that is doing the monitoring. The callbacks should be lightweight
    in order to not build up too many messages in the queue

    The functionality of this class mirrors Dask's structured logs
    feature, which has a similar message => topic => callback
    model running in the client event loop
    """
    def __init__(self, queue: 'WorkerQueue', subscriptions: Dict[str, List[Callable]]):
        self._message_q = queue
        self._subscriptions = subscriptions
        self._thread = None

    def __enter__(self, *args, **kwargs):
        if self._thread is not None:
            raise RuntimeError('Cannot re-enter CommsDispatcher')
        self._thread = threading.Thread(target=self.monitor_queue)
        self._thread.start()

    def __exit__(self, *args, **kwargs):
        if self._thread is None:
            return
        self._message_q.put(('STOP', {}))
        self._thread.join()
        self._thread = None
        # Drain queue just in case
        while True:
            try:
                with self._message_q.get(block=False) as _:
                    ...
            except WorkerQueueEmpty:
                break

    def monitor_queue(self):
        """
        Monitor the queue for messages
        If there are no subscribers this should drain
        messages from the queue as fast as they are recieved
        """
        while True:
            with self._message_q.get(block=True) as ((topic, msg), _):
                if topic == 'STOP':
                    break
                try:
                    for callback in self._subscriptions[topic]:
                        callback(topic, msg)
                except KeyError:
                    pass


class ProgressManager:
    """
    Handle construction and updating of a tqdm.tqdm progress bar
    for a set of UDFTasks, to be completed in any order

    The bar displays as such:

        Partitions: n_complete(n_in_progress) / n_total, ...\
            Frames: [XXXXX..] frames_completed / total_frames ...

    When processing tile stacks, stacks are treated as frames
    as such: (pseudo_frames = tile.size // sig_size)

    The bar will render in a Jupyter notebook as a JS widget
    automcatially via tqdm.auto
    """
    def __init__(self, tasks: Iterable['UDFTask']):
        if not tasks:
            raise ValueError('Cannot display progress for empty tasks')
        # the number of whole frames we expect each task to process
        self._task_max = {t.partition.get_ident(): t.task_frames
                          for t in tasks}
        # _counters is our record of progress on a task,
        # values are floating whole frames processed
        # as in tile mode we can process part of a frame
        self._counters = {k: 0. for k in self._task_max.keys()}
        total_frames = sum(self._task_max.values())
        # For converting tiles to pseudo-frames
        self._sig_size = tasks[0].partition.shape.sig.size
        # Counters for part display
        self._num_complete = 0
        self._num_in_progress = 0
        self._num_total = len(self._counters)
        # Create the bar object
        self._bar = self.make_bar(total_frames)

    def make_bar(self, maxval: int):
        from tqdm.auto import tqdm
        return tqdm(desc=self.get_description(),
                    total=maxval,
                    leave=True)

    def get_description(self) -> str:
        """
        Get the most recent description string for the
        bar, including partition information
        If we know that partitions are in progress
        include this in parentheses after n_completed

        Only called when we start/end a partition
        """
        if self._num_in_progress:
            return (f'Partitions {self._num_complete}({self._num_in_progress})'
                    f'/{self._num_total}, Frames')
        else:
            return (f'Partitions {self._num_complete}'
                    f'/{self._num_total}, Frames')

    def finalize_task(self, task: 'UDFTask'):
        """
        When a task completes and we recieve its results on
        the main node, this is called to update the partition
        progress counters and frame counter in case we didn't
        recieve a complete history of the partition yet
        """
        topic = 'partition_complete'
        ident = task.partition.get_ident()
        message = {'ident': task.partition.get_ident()}
        if ident in self._task_max:
            self.handle_end_task(topic, message)

    def update_description(self):
        self._bar.set_description(self.get_description())

    def update_bar(self, n: int):
        """
        Increment the progress bar, clipped to remain within the bar maximum
        """
        max_update = self._bar.total - self._bar.n
        if max_update > 0:
            self._bar.update(min(n, max_update))

    def close(self):
        self.update_description()
        self._bar.close()

    def connect(self, comms: 'TaskCommHandler'):
        """
        Register the callbacks on this class with the TaskCommHandler
        which will be dispatching messages recieved from the tasks
        """
        comms.subscribe('partition_start', self.handle_start_task)
        comms.subscribe('partition_complete', self.handle_end_task)
        comms.subscribe('tile_complete', self.handle_tile_update)

    def handle_start_task(self, topic: str, message: Dict[str, Any]):
        """
        Increment the num_in_progress counter

        # NOTE An extension to this would be to track
        the identities of partitions in progress / completed
        for a richer display / more accurate accounting
        """
        if topic != 'partition_start':
            raise RuntimeError('Unrecognized topic')
        self._num_in_progress += 1
        self.update_description()

    def handle_end_task(self, topic: str, message: Dict[str, Any]):
        """
        Increment the counter for the task to the max value
        and update the various counters / description
        """
        if topic != 'partition_complete':
            raise RuntimeError('Unrecognized topic')
        t_id = message['ident']
        remain = self._task_max[t_id] - int(self._counters[t_id])
        if remain:
            self.update_bar(remain)
            self._counters[t_id] = self._task_max[t_id]
        self._num_complete += 1
        self._num_in_progress = max(0, self._num_in_progress - 1)
        self.update_description()

    def handle_tile_update(self, topic: str, message: Dict[str, Any]):
        """
        Update the frame progress counter for the task
        and push the increment to the tqdm bar

        Tile stacks are converted to pseudo-frames via the sig_size
        """
        if topic != 'tile_complete':
            raise RuntimeError('Unrecognized topic')
        t_id = message['ident']
        if self._counters[t_id] >= self._task_max[t_id]:
            return
        elements = message['elements']
        pframes = elements / self._sig_size
        if int(pframes):
            self.update_bar(int(pframes))
        self._counters[t_id] += pframes


class PartitionTrackerNoOp:
    """
    A no-op class matching the PartitionProgressTracker interface
    Used when progress == False to avoid any additional overhead
    """
    def signal_start(self, *args, **kwargs):
        ...

    def signal_tile_complete(self, *args, **kwargs):
        ...

    def signal_complete(self, *args, **kwargs):
        ...


class PartitionProgressTracker(PartitionTrackerNoOp):
    """
    Tracks the tile processing speed of a Partition and
    dispatches messages via the worker_context.signal() method
    under certain conditions

    Parameters
    ----------
    partition : Partition
        The partition to track progress for
    roi : Optional[np.ndarray]
        The roi associated with this UDF run
    threshold_part_time : float, optional
        The total partition processing time below
        which no messages are sent, by default 4 seconds.
    min_message_interval : float, optional
        The minumum time between messages, by default 1 second.
    """
    def __init__(
                self,
                partition: 'Partition',
                roi: Optional['np.ndarray'],
                threshold_part_time: float = 4.,
                min_message_interval: float = 1.,
            ):
        self._ident = partition.get_ident()
        try:
            self._worker_context = partition._worker_context
        except AttributeError:
            self._worker_context = None

        # Counters to track / rate-limit messages
        self._elements_complete = 0
        self._last_message_t = None
        self._min_message_interval = min_message_interval
        # Size of data in partition (accounting for ROI)
        nel = partition.get_frame_count(roi) * partition.meta.shape.sig.size
        self._threshold_rate = nel / threshold_part_time

    def signal_start(self):
        """
        Signal that the partition has begun processing
        """
        if self._worker_context is None:
            return
        self._worker_context.signal(
            self._ident,
            'partition_start',
            {},
        )

    def signal_tile_complete(self, tile: 'DataTile'):
        """
        Register that tile.size more elements have been processed
        and if certain condition are met, send a signal
        """
        if self._worker_context is None:
            return

        send, elements = self.should_send_progress(tile.size)
        if send:
            self._worker_context.signal(
                self._ident,
                'tile_complete',
                {'elements': elements},
            )

    def signal_complete(self):
        """
        Signal that the partition has completed processing

        This is not currently called as partition completion
        is registered on the main node as a fallback
        """
        if self._worker_context is None:
            return
        self._worker_context.signal(
            self._ident,
            'partition_complete',
            {},
        )

    def should_send_progress(self, elements: int) -> Tuple[bool, int]:
        """
        Given the number elements of data that have been processed since
        the last message was sent, decide if a signal should be sent to the
        main node about the partition progress

        The decision is made based on the average (elements / second) and time
        since last message, thresholds were set in the class constructor
        """
        current_t = time.time()
        self._elements_complete += elements

        if self._last_message_t is None:
            # Never send a message for the first tile stack
            # as this might have warmup overheads associated
            # Include the first elements in the history, however,
            # to give a better accounting. The first tile stack
            # is essentially treated as 'free'.
            self._last_message_t = current_t
            return False, 0

        time_since_last_m = current_t - self._last_message_t
        avg_rate = self._elements_complete / time_since_last_m

        part_is_slow = avg_rate < self._threshold_rate
        not_rate_limited = time_since_last_m > self._min_message_interval
        if part_is_slow and not_rate_limited:
            completed_elements = self._elements_complete
            self._elements_complete = 0
            self._last_message = current_t
            return True, completed_elements

        return False, 0

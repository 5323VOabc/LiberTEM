import queue
from typing import (
    Callable, Generator, Optional, Any, Iterable, TYPE_CHECKING, Tuple,
    TypeVar, Type,
)
from contextlib import contextmanager
import multiprocessing as mp

import cloudpickle
import numpy as np
from typing_extensions import Protocol

from libertem.common.scheduler import WorkerSet
from libertem.common.threading import set_num_threads, mitigations
from libertem.io.dataset.base import Partition


if TYPE_CHECKING:
    from libertem.udf.base import UDFParams, UDFRunner
    from opentelemetry.trace import SpanContext


class ExecutorError(Exception):
    pass


class JobCancelledError(Exception):
    """
    raised by async executors in run_tasks() or run_each_partition() if the task was cancelled
    """
    pass


class Environment:
    '''
    Create the environment to run a task, in particular thread count.
    '''
    def __init__(
        self,
        threads_per_worker: Optional[int],
        threaded_executor: bool,
        worker_context: Optional["WorkerContext"] = None,
    ):
        self._threads_per_worker = threads_per_worker
        self._threaded_executor = threaded_executor
        self._worker_context = worker_context

    @property
    def threads_per_worker(self) -> Optional[int]:
        """
        int or None : number of threads that a UDF is allowed to use in the `process_*` method.
                      For numba, pyfftw, OMP, MKL, OpenBLAS, this limit is set automatically;
                      this property can be used for other cases, like manually creating
                      thread pools.
                      None means no limit is set, and the UDF can use any number of threads
                      it deems necessary (should be limited to system limits, of course).

        See also: :func:`libertem.common.threading.set_num_threads`

        .. versionadded:: 0.7.0
        """
        return self._threads_per_worker

    @property
    def threaded_executor(self) -> bool:
        """
        Whether or not the executor uses threading for parallelism.
        If this flag is set, mitigations for common threading issues are
        automatically applied.
        """
        return self._threaded_executor

    @property
    def worker_context(self) -> Optional["WorkerContext"]:
        """
        A :code:`WorkerContext` instance, if available, as supplied
        by the `JobExecutor`. This is used to manage streaming communication
        between the main process and the workers. If :code:`None` is
        returned, streaming communication with workers is not available.
        """
        return self._worker_context

    @contextmanager
    def enter(self):
        """
        Note: we are using the @contextmanager decorator here,
        because with separate `__enter__`, `__exit__` methods,
        we can't easily delegate to `set_num_threads`, or other
        contextmanagers that may come later.
        """
        with set_num_threads(self._threads_per_worker):
            if self.threaded_executor:
                with mitigations():
                    yield self
            else:
                yield self


class TaskProtocol(Protocol):
    '''
    Interface for tasks
    '''
    def __call__(self, params: "UDFParams", env: Environment):
        pass

    def get_tracing_span_context(self) -> "SpanContext":
        ...

    def get_partition(self) -> Partition:
        ...


T = TypeVar('T')
V = TypeVar('V')


class JobExecutor:
    '''
    Interface to execute functions on workers.
    '''
    def run_function(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """
        run a callable `fn` on any worker
        """
        raise NotImplementedError()

    @contextmanager
    def scatter(self, obj):
        '''
        Scatter :code:`obj` throughout the cluster

        Parameters
        ----------

        obj
            Some kind of Python object or variable

        Returns
        -------
        handle
            Handle for the scattered :code:`obj`
        '''
        raise NotImplementedError()

    def run_tasks(
        self,
        tasks: Iterable[TaskProtocol],
        params_handle: Any,
        cancel_id: Any,
        controller: "TaskCommHandler",
    ):
        """
        Run the tasks with the given parameters

        Parameters
        ----------
        tasks
            The tasks to be run
        params_handle : [type]
            A handle for the task parameters, as returned from :meth:`JobExecutor.scatter`
        cancel_id
            An identifier which can be used for cancelling all tasks together. The
            same identifier should be passed to :meth:`AsyncJobExecutor.cancel`
        """
        raise NotImplementedError()

    def run_each_partition(self, partitions, fn, all_nodes=False):
        """
        Run `fn` for all partitions. Yields results in order of completion.

        Parameters
        ----------

        partitions : List[Partition]
            List of relevant partitions.

        fn : callable
            Function to call, will get the partition as first and only argument.

        all_nodes : bool
            If all_nodes is True, run the function on all nodes that have this partition,
            otherwise run on any node that has the partition. If a partition has no location,
            the function will not be run for that partition if `all_nodes` is True, otherwise
            it will be run on any node.
        """
        raise NotImplementedError()

    def map(self, fn: Callable[[V], T], iterable: Iterable[V]) -> Iterable[T]:
        """
        Run a callable `fn` for each element in `iterable`, on arbitrary worker nodes.

        Parameters
        ----------

        fn : callable
            Function to call. Should accept exactly one parameter.

        iterable : Iterable
            Which elements to call the function on.
        """
        raise NotImplementedError()

    def run_each_host(self, fn, *args, **kwargs):
        """
        Run a callable `fn` once on each host, gathering all results into a dict host -> result


        Parameters
        ----------

        fn : callable
            Function to call

        *args
            Arguments for fn

        **kwargs
            Keyword arguments for fn
        """
        raise NotImplementedError()

    def run_each_worker(self, fn, *args, **kwargs):
        """
        Run :code:`fn` on each worker process, and pass :code:`*args`,
        :code:`**kwargs` to it.

        Useful, for example, if you need to prepare the environment of
        each Python interpreter, warm up per-process caches etc.

        Parameters
        ----------
        fn : callable
            Function to call
        \\*args
            Arguments for fn
        \\*\\*kwargs
            Keyword arguments for fn

        Returns
        -------
        dict
            Return values keyed by worker name (executor-specific)
        """
        raise NotImplementedError()

    def close(self):
        """
        cleanup resources used by this executor, if any
        """

    def get_available_workers(self) -> WorkerSet:
        """
        Returns a WorkerSet that contains the available workers

        Each worker should correspond to a "worker process", so if the executor
        is using multiple processes or threads, each process/thread should be
        included in this list.
        """
        raise NotImplementedError()

    def get_resource_details(self):
        """
        Returns a list of dicts with cluster details

        key of the dictionary:
            host: ip address or hostname where the worker is running
        """
        raise NotImplementedError()

    def ensure_sync(self):
        """
        Returns a synchronous executor, incase of a `JobExecutor` we just
        return `self; in case of `AsyncJobExecutor` below more work is needed!
        """
        return self

    def ensure_async(self, pool=None):
        """
        Returns an asynchronous executor; by default just wrap into `AsyncAdapter`.
        """
        raise NotImplementedError()

    def modify_buffer_type(self, buf):
        """
        Allow the executor to modify result buffers if necessary

        Currently only called for buffers on the main node
        """
        return buf

    def get_udf_runner(self) -> Type['UDFRunner']:
        raise NotImplementedError


class AsyncJobExecutor:
    '''
    Async version of :class:`JobExecutor`.
    '''
    async def run_tasks(self, tasks, params_handle, cancel_id):
        """
        Run a number of Tasks, yielding (result, task) tuples
        """
        raise NotImplementedError()

    async def run_function(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """
        Run a callable `fn` on any worker
        """
        raise NotImplementedError()

    async def run_each_partition(self, partitions, fn, all_nodes=False):
        raise NotImplementedError()

    async def map(self, fn, iterable):
        """
        Run a callable `fn` for each item in iterable, on arbitrary worker nodes

        Parameters
        ----------

        fn : callable
            Function to call. Should accept exactly one parameter.

        iterable : Iterable
            Which elements to call the function on.
        """
        raise NotImplementedError()

    async def run_each_host(self, fn, *args, **kwargs):
        raise NotImplementedError()

    async def run_each_worker(self, fn, *args, **kwargs):
        """
        Run :code:`fn` on each worker process, and pass :code:`*args`, :code:`**kwargs` to it.

        Useful, for example, if you need to prepare the environment of
        each Python interpreter, warm up per-process caches etc.

        Parameters
        ----------
        fn : callable
            Function to call
        \\*args
            Arguments for fn

        \\*\\*kwargs
            Keyword arguments for fn
        """
        raise NotImplementedError()

    async def close(self):
        """
        Cleanup resources used by this executor, if any.
        """

    async def cancel(self, cancel_id):
        """
        cancel execution identified by `cancel_id`
        """
        pass

    async def get_available_workers(self):
        raise NotImplementedError()

    async def get_resource_details(self):
        raise NotImplementedError()

    def ensure_sync(self):
        raise NotImplementedError()

    def ensure_async(self, pool=None):
        """
        Returns an asynchronous executor; by default just return `self`.
        """
        return self

    def get_udf_runner(self) -> Type['UDFRunner']:
        raise NotImplementedError()


class WorkerQueueEmpty(Exception):
    """
    A non-blocking get was called on an empty queue, or a blocking `get` with a
    non-zero timeout timed out.
    """
    pass


class WorkerQueue:
    @contextmanager
    def get(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> Generator[Tuple[Any, memoryview], None, None]:
        raise NotImplementedError()

    def put(self, header: Any, payload: Optional[memoryview] = None):
        raise NotImplementedError()

    @contextmanager
    def put_nocopy(self, header: Any, size: int) -> Generator[memoryview, None, None]:
        """
        Put data into the queue, without an additional copy.
        This will yield a writable `memoryview`, instead of requiring that
        the data to be sent is already available as a `memoryview` and
        copying into its final destination, as is the case in `put`.

        This can be useful, for example, if you need to perform any kind
        of operation that writes into a buffer. For example:

        >>> q = SomeWorkerQueueImpl()  # doctest: +SKIP
        >>> with q.put_nocopy(1024) as send_buf:  # doctest: +SKIP
        ...     # NOTE: in reality, need to handle n_bytes < 1024
        ...     n_bytes = some_socket.recvinto(send_buf)

        If the data you receive is already in a shared memory segment,
        you can send the handle as part of the `header` and use the normal `put`
        method instead.

        Note: for releasing memory, it might be necessary to implement another
        queue that synchronizes the release process.
        """
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()


class SimpleWorkerQueue(WorkerQueue):
    """
    A :code:`WorkerQueue` that uses a threading :code:`Queue` under the hood.
    """
    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()

    def put(self, header, payload: Optional[memoryview] = None):
        self.q.put((header, payload))

    @contextmanager
    def put_nocopy(self, header: Any, size: int) -> Generator[memoryview, None, None]:
        payload = np.zeros(size, dtype=np.uint8)
        yield memoryview(payload)
        self.q.put((header, payload))

    @contextmanager
    def get(self, block: bool = True, timeout: Optional[float] = None):
        try:
            res = self.q.get(block=block, timeout=timeout)
            yield res
        except queue.Empty:
            raise WorkerQueueEmpty()

    def close(self):
        pass


class SimpleMPWorkerQueue(WorkerQueue):
    """
    A :code:`WorkerQueue` that uses a :code:`mp.Queue` under the hood.
    """
    def __init__(self) -> None:
        self._mp_ctx = mp.get_context("spawn")
        self.q: mp.Queue = self._mp_ctx.Queue()
        self._closed = False

    def put(self, header, payload: Optional[memoryview] = None):
        header_serialized = cloudpickle.dumps(header)
        payload_serialized = cloudpickle.dumps(payload)
        self.q.put((header_serialized, payload_serialized))

    @contextmanager
    def put_nocopy(self, header: Any, size: int) -> Generator[memoryview, None, None]:
        payload = np.zeros(size, dtype=np.uint8)
        yield memoryview(payload)
        self.q.put((header, payload))

    @contextmanager
    def get(self, block: bool = True, timeout: Optional[float] = None):
        try:
            res = self.q.get(block=block, timeout=timeout)
            yield (cloudpickle.loads(res[0]), cloudpickle.loads(res[1]))
        except queue.Empty:
            raise WorkerQueueEmpty()

    def close(self):
        if not self._closed:
            while True:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
            self.q.close()
            self.q.join_thread()
            self._closed = True


class WorkerContext:
    """
    A :code:`WorkerContext` is used to manage streaming communication between
    the main process and the workers.
    """
    def get_worker_queue(self) -> WorkerQueue:
        raise NotImplementedError()


class TaskCommHandler:
    """
    This is the interface that is implemented by the acquisition object
    to allow streaming communication with workers.
    """
    def handle_task(self, task: TaskProtocol, queue: WorkerQueue):
        """
        Handle the streaming connunication for the given :code:`task`
        using the provided :code:`queue`. This function should
        block until the communication for the given task has finished.
        It may be run in a background thread on the main node,
        or synchronously, depending on the :code:`JobExecutor`.

        Parameters
        ----------
        task : TaskProtocol

        queue : WorkerQueue
            A queue used to communicate with the worker process
            using an acquisition-specific protocol. The `Partition`
            in the worker has access to this queue, too, and can
            communicate using a data-source specific protocol.

            The `MainController` and the `Partition` are tightly
            coupled by this protocol, and the queue needs to be "clean"
            after the data for a given task has been exchanged.
        """
        ...

    def start(self):
        """
        A lifecycle method that is called before any task has been
        run.
        """
        ...

    def done(self):
        """
        A lifecycle method that is called after all tasks have benn
        run.
        """
        ...


class NoopCommHandler(TaskCommHandler):
    """
    A `MainController` that doesn't perform any action, and doesn't
    stream any data.
    """
    def handle_task(self, task: TaskProtocol, queue: WorkerQueue):
        pass

    def start(self):
        pass

    def done(self):
        pass

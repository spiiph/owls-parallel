"""Provides simple and portable parallelization facilities.
"""


# Future imports to support fancy print() on Python 2.x
from __future__ import print_function

# System imports
import os
import threading
from collections import defaultdict
from functools import wraps
from sys import stdout

# Six imports
from six import iteritems
from six.moves import queue

# owls-cache imports
# HACK: We use a private function, but owls-parallel is intrinsically linked to
# owls-cache since it is used as a transport mechanism, so I guess we'll live
# with it.
from owls_cache.persistent import _get_cache


# Export the owls-parallel version
__version__ = '0.0.1'


# Determine if the process' output is being redirected
_output_is_tty = (os.fstat(0) == os.fstat(1))


# Create a thread-local variable to track whether or not the current thread is
# in capture mode
_thread_local = threading.local()


# Utility function to get the current parallelizer
_get_parallelizer = lambda: getattr(_thread_local, 'owls_parallelizer', None)


# Utility function to set the current parallelizer
def _set_parallelizer(parallelizer):
    _thread_local.owls_parallelizer = parallelizer


# The default batch executer
def _batcher(function, args_kwargs):
    for args, kwargs in args_kwargs:
        function(*args, **kwargs)


def parallelized(mocker, mapper, batcher = _batcher, parallel_pass = 1):
    """Decorator to add parallelization functionality to a callable.

    The underlying function, or some function further down the call stack, must
    be wrapped with an @owls_cache.persistent.cached directive, or the result
    of the parallel computation will be lost.

    The @parallelized decorator must also be the outermost decorator used on
    the function.

    Finally, the underlying function must also be importable by name on
    engines.

    Args:
        mocker: A function which takes the same arguments as the underlying
            function and returns a dummy default value which has the same
            algebraic as the actual return value, but which can be computed
            quickly and returned whilst in capture mode
        mapper: A function which accepts the same arguments as the underlying
            function and maps them to a hashable tuple of values, which will
            then be hashed to act as a grouping key for parallel jobs.  This
            can be useful to, e.g., group jobs in a manner conducive to
            caching.  If the argument types to the underlying function are not
            hashable (e.g. they are lists or dictionaries), then this function
            provides a mechanism by which to convert them to hashable types
            (e.g. tuples).
        batcher: A function which can be called with a function and a list of
            (args, kwargs) tuples and call the function with each of the
            arguments.  Defaults to a naive implementation which simply
            iterates through args/kwargs and calls the function, but users can
            replace this with a function which calls the underlying function in
            a more optimal manner (e.g. one conducive to caching).  This
            function must be pickleable (i.e. importable by name).

    Returns:
        A version of the function which supports parallelization using a job
        capture paradigm.
    """
    # Create the decorator
    def decorator(f):
        # Create the wrapper function
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Grab the current parallelizer
            parallelizer = _get_parallelizer()

            # If we're not in capture mode or if we're in the wrong pass,
            # then we're done
            if parallelizer is None or \
               parallelizer.parallel_pass() != parallel_pass:
                return f(*args, **kwargs)

            # Otherwise, we are in capture mode, so we need to compute the key
            # by which to organize this job
            key = hash(mapper(*args, **kwargs))

            # Register the job with the parallelizer
            # NOTE: We register the *wrapper* function, because it is what will
            # be assigned to the name of the function
            parallelizer._record(key, batcher, wrapper, args, kwargs,
                    parallel_pass)

            # Return a dummy value
            return mocker(*args, **kwargs)

        # Return the wrapper
        return wrapper

    # Return the decorator
    return decorator


# Convenience function to recursively convert defaultdict objects to normal
# dictionary objects so they can be pickled (if we use lambdas in the
# constructor of the defaultdict, they won't pickle)
def _dict_convert(dictionary):
    # Create the result
    result = {}

    # Add items
    for k, v in iteritems(dictionary):
        if isinstance(v, defaultdict):
            result[k] = _dict_convert(v)
        else:
            result[k] = v

    # All done
    return result


class ParallelizedEnvironment(object):
    """An environment in which functions wrapped with the @parallelized
    directive are captured when called and then executed on a given backend.
    """

    def __init__(self, backend, monitor_interval = 5):
        """Creates a new instance of the ParallelizedEnvironment class.

        Args:
            backend: The backend to use for parallelization (None for no
                parallelization)
            monitor_interval: How often to query/print progress, in seconds
                (defaults to 5)
        """
        # Create variables to track run state
        self._captured = False
        self._computed = False
        self._parallel_pass = 1

        self._initialize_jobs()

        # Store the backend and progress interval
        self._backend = backend
        self._monitor_interval = monitor_interval

    def _initialize_jobs(self):
        """Initialize the job register. Called before each pass.
        """
        # Create the list of register jobs.  Structure is:
        # {
        # {
        #     key: {
        #         batcher: {
        #             function: [
        #                 (args1, kwargs1),
        #                 ...
        #                 (argsN, kwargsN),
        #             ],
        #             ...
        #         },
        #         ...
        #     },
        #     ...
        # }
        # }
        self._jobs = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )

    def _record(self, key, batcher, function, args, kwargs, parallel_pass):
        """Adds a new job to be computed on the parallel backend.

        Args:
            key: The key by which to group the job for optimal performance.
                The key must be hashable.
            batcher: The batch computation algorithm to use
            function: The function to execute
            args: The arguments to the function
            kwargs: The keyword arguments to the function
            parallel_pass: Which pass the parallelized function belongs to
        """
        if self._parallel_pass == parallel_pass:
            self._jobs[key][batcher][function].append((args, kwargs))

    def _compute(self, progress):
        """Runs computation and blocks until completion, optionally printing
        progress.

        Args:
            progress: Whether or not to print progress information
        """
        # Grab current persistent cache and validate it
        cache = _get_cache()
        if cache is None:
            raise RuntimeError('not inside a cached context')

        # Create a queue on which backends can notify us of new results
        notification_queue = queue.Queue()

        # Create a monitoring function, which waits for notifications, but also
        # allows for regular polling
        def monitor():
            try:
                notification_queue.get(timeout = self._monitor_interval)
            except queue.Empty:
                pass
            return True

        # Create a notification callback
        callback = lambda: notification_queue.put(None)

        # Start jobs
        all_jobs = self._backend.start(cache,
                                       _dict_convert(self._jobs),
                                       callback)

        # Compute printing parameters
        n_blocks = 50
        percentage_width = 3
        job_count_width = len(str(len(all_jobs)))
        format_string = \
            '{{}}[{{:<{0}}}] {{:>{1}.0f}}% ({{:>{2}}}/{{:>{2}}})'.format(
                n_blocks,
                percentage_width,
                job_count_width
            )

        # Monitor jobs
        remaining_jobs = all_jobs
        previous_completed = 0
        initial = True
        while monitor():
            # Grab the unfinished jobs
            remaining_jobs = self._backend.prune(remaining_jobs)

            # Compute progress
            total = len(all_jobs)
            completed = total - len(remaining_jobs)

            # Print the percentage if necessary
            if progress and ((completed != previous_completed) or initial):
                # Compute display information
                fraction_completed = float(completed) / total
                filled_blocks = int(fraction_completed * n_blocks)

                # Print differently based on the output device
                print(
                    format_string.format(
                        '\r' if _output_is_tty else '',
                        '#' * filled_blocks,
                        fraction_completed * 100,
                        completed,
                        total),
                    end = '' if _output_is_tty else os.linesep
                )
                stdout.flush()

            # Update state
            previous_completed = completed
            initial = False

            # If we're done, leave this loop
            if completed == total:
                if progress:
                    print('')
                break

    def capturing(self):
        """Returns True if the environment is in capture mode, False
        otherwise.
        """
        return self._captured and not self._computed

    def computed(self):
        """Returns True if the environment has finished computing, False
        otherwise.
        """
        return self._computed

    def parallel_pass(self):
        """Returns the current pass
        """
        return self._parallel_pass

    def run(self, progress = True, passes = 1):
        """Manages execution in a parallelized environment, optionally printing
        progress.

        This method is designed to wrap code which should be parallelized in a
        while loop, e.g.:

            # Execute in a cached environment (required)
            with caching_into(my_cache):
                # Compute results in a parallel manner
                parallelizer = ParallelizedEnvironment(...)
                while parallelizer.run():
                    # Peform computation which should be parallelized
                    value_1 = function_1()
                    value_2 = function_2()

                # Use results
                ...

        On the first call, this method sets the global state (within the
        thread) to a mode where all @parallelized calls are captured for later
        computation, and the method returns True.

        On the second call, the captured computations are computed in parallel,
        and this method returns True.

        On the third (or later) call, this method is a no-op and returns False.

        Args:
            progress: Whether or not to print progress information
            passes: How many parallel computation passes to run

        Returns:
            True or False depending on run state.
        """
        # Handle based on state
        if self._backend is None and not (self._captured or self._computed):
            # If we have no backend, then mark ourselves as completed the first
            # time through, since we won't be doing any parallelization
            if progress:
                print('Parallelization unavailable, computing...')
            self._captured = True
            self._computed = True
            return True
        elif not self._captured:
            # If we haven't captured yet, then set the parallelizer for
            # capturing and allow the loop to run
            if progress:
                print('Capturing for parallelization (pass {0})...'.\
                        format(self._parallel_pass))
            self._captured = True
            self._computed = False
            _set_parallelizer(self)
            return True
        elif not self._computed:
            # If we have already captured but haven't computed, then unset the
            # parallelizer and run the computations in a blocking manner, then
            # allow the loop to run to pull values out of the persistent cache
            if progress:
                print('Computing in parallel (pass {0})...'.\
                        format(self._parallel_pass))
            self._computed = True
            _set_parallelizer(None)
            self._compute(progress)
            if self._parallel_pass < passes:
                self._captured = False
                self._parallel_pass += 1
                self._initialize_jobs()
            # TODO: Add an option to return false after all passes have
            # completed the computation step
            return True
        return False

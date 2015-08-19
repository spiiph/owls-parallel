"""Provides a multiprocessing-based parallelization backend.
"""

# HACK: Use absolute_import behavior to get around module having the same name
# as the global multiprocessing module
from __future__ import absolute_import

# System imports
from multiprocessing import Pool
from traceback import print_exc

# Six imports
from six import iteritems, itervalues

# owls-cache imports
from owls_cache.persistent import caching_into

# owls-parallel imports
from owls_parallel.backends import ParallelizationBackend


# Create a function to execute jobs on the cluster
def _run(cache, job):
    with caching_into(cache):
        for batcher, calls in iteritems(job):
            for function, args_kwargs in iteritems(calls):
                try:
                    batcher(function, args_kwargs)
                except Exception:
                    print_exc()
                    raise


class MultiprocessingParallelizationBackend(ParallelizationBackend):
    """A parallelization backend which uses a multiprocessing pool to compute
    results.
    """

    def __init__(self, *args, **kwargs):
        """Initializes a new instance of the
        MultiprocessingParallelizationBackend.

        Args: The same as the multiprocessing.Pool class
        """
        # Create the processing pool
        self._cluster = Pool(*args, **kwargs)

    def start(self, cache, job_specs, callback):
        """Run jobs on the backend, blocking until their completion.

        Args:
            cache: The persistent cache which should be set on the backend
            job_specs: The job specification (see
                owls_parallel.backends.ParallelizationBackend)
            callback: The job notification callback
        """
        return [self._cluster.apply_async(_run,
                                          (cache, j),
                                          callback = lambda x: callback())
                for j
                in itervalues(job_specs)]

    def prune(self, jobs):
        """Prunes a collection of jobs by pruning those which are complete.

        The input collection should not be modified.

        Args:
            jobs: A collection of jobs to prune

        Returns:
            A new collection of jobs which are still incomplete.
        """
        # Extract unfinished jobs, and re-raise any remote exceptions
        result = []
        for j in jobs:
            if j.ready():
                # This will re-raise remotely-raised exceptions locally
                j.get()
            else:
                result.append(j)

        # All done
        return result

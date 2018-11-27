"""
.. module:: strategy
   :synopsis: Parallel synchronous optimization strategy

.. moduleauthor:: David Eriksson <dme65@cornell.edu>
                David Bindel <bindel@cornell.edu>,

:Module: strategy
:Author: David Eriksson <dme65@cornell.edu>
        David Bindel <bindel@cornell.edu>,
"""

import abc
import dill
import logging
import math
import numpy as np
import os
import time

from poap.strategy import BaseStrategy, Proposal, RetryStrategy
from pySOT.surrogate import RBFInterpolant, CubicKernel, LinearTail
from pySOT.adaptive_sampling import make_srbf
from pySOT.experimental_design import SymmetricLatinHypercube, LatinHypercube
from pySOT.utils import from_unit_box, round_vars

# Get module-level logger
logger = logging.getLogger(__name__)


class RandomSampling(BaseStrategy):
    def __init__(self, opt_prob, max_evals):
        self.opt_prob = opt_prob
        self.max_evals = max_evals
        self.retry = RetryStrategy()
        for _ in range(max_evals):  # Generate the random points
            x = np.random.uniform(low=opt_prob.lb, high=opt_prob.ub)
            proposal = self.propose_eval(x)
            self.retry.rput(proposal)

    def propose_action(self):
        """Propose an action based on outstanding points."""
        if not self.retry.empty():  # Propose next point
            return self.retry.get()
        elif self.retry.num_eval_outstanding == 0:  # Budget exhausted and nothing pending
            return self.propose_terminate()


class SurrogateBaseStrategy(BaseStrategy):
    __metaclass__ = abc.ABCMeta
    """Surrogate base strategy

    Once the budget of max_evals function evaluations have been assigned,
    no further evaluations are assigned to processors. The code returns
    once all evaluations are completed.
    """

    def __init__(self, max_evals, opt_prob, exp_design=None, surrogate=None,
                 asynchronous=True, batch_size=None, stopping_criterion=None, 
                 extra=None):
        """Skeleton for surrogate optimization.

        Args:
            max_evals: Maximum number of evaluations (or negative number of seconds)
            opt_prob: Optimization problem object
            exp_design: Experimental design object
            surrogate: Surrogate model object
            asynchronous: True if asynchronous, False if batch synchronous
            batch_size: Size of each batch, not used if asynchronous == True
            stopping_criterion: Stopping criterion
            extra: Extra points (and values) to be added to the experimental design
        """

        # Check stopping criterion
        self.start_time = time.time()
        if max_evals < 0:  # Time budget
            self.maxeval = np.inf
            self.time_budget = np.abs(max_evals)
        else:
            self.maxeval = max_evals
            self.time_budget = np.inf
        self.max_evals = np.abs(max_evals)

        self.stopping_criterion = stopping_criterion
        self.proposal_counter = 0
        self.terminate = False
        self.asynchronous = asynchronous
        self.batch_size = batch_size

        self.opt_prob = opt_prob
        self.surrogate = surrogate
        if self.surrogate is None:
            self.surrogate = RBFInterpolant(dim=opt_prob.dim, kernel=CubicKernel(),
                                            tail=LinearTail(opt_prob.dim))

        # Default to generate sampling points using Symmetric Latin Hypercube
        if exp_design is None:
            exp_design = SymmetricLatinHypercube(dim=opt_prob.dim, npts=2*(opt_prob.dim+1))
        self.exp_design = exp_design

        # Sampler state
        self.accepted_count = 0
        self.rejected_count = 0
    
        # Initial design info
        self.extra = extra
        self.batch_queue = []   # Unassigned points in initial experiment
        self.init_pending = 0   # Number of outstanding initial fevals
        self.phase = 1          # 1 for initial, 2 for adaptive

        # Budgeting state
        self.num_evals = 0               # Number of completed fevals
        self.feval_budget = max_evals  # Remaining feval budget
        self.feval_pending = 0         # Number of outstanding fevals

        # Completed evaluations
        self.X = np.empty([0, opt_prob.dim])
        self.fX = np.empty([0, 1])
        self.Xpend = np.empty([0, opt_prob.dim])
        self.fevals = []

        # Check inputs (implemented by each strategy)
        self.check_input()

        # Start with first experimental design
        self.sample_initial()

    @abc.abstractmethod  # pragma: no cover
    def generate_evals(self, num_pts):
        pass

    def check_input(self):
        """Todo: Write this. """
        pass

    def save(self, fname):
        """Save the state in a 3-step procedure
            1) Save to temp file
            2) Move temp file to save file
            3) Remove temp file
        """
        temp_fname = "temp_" + fname
        with open(temp_fname, 'wb') as output:
            dill.dump(self, output, dill.HIGHEST_PROTOCOL)
        os.rename(temp_fname, fname)

    def resume(self):
        """Resuming a terminated run."""
        self.feval_pending = 0

    def log_completion(self, record):
        """Record a completed evaluation to the log.

        :param record: Record of the function evaluation
        """
        xstr = np.array_str(record.params[0], max_line_width=np.inf,
                            precision=5, suppress_small=True)
        logger.info("{} {:.3e} @ {}".format(self.num_evals, record.value, xstr))

    def sample_initial(self):
        """Generate and queue an initial experimental design."""
        logger.info("=== Start ===")
        self.surrogate.reset()

        start_sample = self.exp_design.generate_points()
        assert start_sample.shape[1] == self.opt_prob.dim, \
            "Dimension mismatch between problem and experimental design"
        start_sample = from_unit_box(start_sample, self.opt_prob.lb, self.opt_prob.ub)
        start_sample = round_vars(start_sample, self.opt_prob.int_var,
                                  self.opt_prob.lb, self.opt_prob.ub)

        for j in range(start_sample.shape[0]):
            self.batch_queue.append(start_sample[j, :])

    def propose_action(self):
        """Propose an action.

        NB: We allow workers to continue to the adaptive phase if the initial 
        queue is empty. This implies that we need enough points in the experimental 
        design for us to construct a surrogate.
        """

        current_time = time.time()
        if self.num_evals >= self.maxeval or self.terminate or \
                (current_time - self.start_time) >= self.time_budget:
            if self.feval_pending == 0:  # Only terminate if nothing is pending
                return Proposal('terminate')
        elif self.batch_queue:  # Propose point from the batch_queue
            if self.phase == 1:
                return self.init_proposal()
            else:
                return self.adapt_proposal()
        else:  # Make new proposal in the adaptive phase
            self.phase == 2
            if self.asynchronous:
                self.generate_evals(num_pts=1)
                return self.adapt_proposal()
            elif self.feval_pending == 0:  # Only propose a batch if nothing is pending
                num_pts = min(self.batch_size, self.max_evals - self.num_evals)
                self.generate_evals(num_pts=num_pts)
                for _ in range(self.batch_size):
                    return self.adapt_proposal()

    def make_proposal(self, x):
        """Create proposal and update counters and budgets."""
        proposal = Proposal('eval', x)
        self.feval_budget -= 1
        self.feval_pending += 1
        self.Xpend = np.vstack((self.Xpend, np.copy(x)))
        return proposal

    def remove_pending(self, x):
        idx = np.where((self.Xpend == x).all(axis=1))
        self.Xpend = np.delete(self.Xpend, idx, axis=0)

    # == Processing in initial phase ==

    def init_proposal(self):
        """Propose a point from the initial experimental design."""
        proposal = self.make_proposal(self.batch_queue.pop())
        proposal.add_callback(self.on_initial_proposal)
        self.init_pending += 1
        return proposal

    def on_initial_proposal(self, proposal):
        """Handle accept/reject of proposal from initial design."""
        if proposal.accepted:
            self.on_initial_accepted(proposal)
        else:
            self.on_initial_rejected(proposal)

    def on_initial_accepted(self, proposal):
        """Handle proposal accept from initial design."""
        self.accepted_count += 1
        proposal.record.add_callback(self.on_initial_update)

    def on_initial_rejected(self, proposal):
        """Handle proposal rejection from initial design."""
        self.rejected_count += 1
        self.feval_budget += 1
        self.feval_pending -= 1
        self.init_pending -= 1
        xx = proposal.args[0]
        self.batch_queue.append(xx)
        self.Xpend = np.vstack((self.Xpend, np.copy(xx)))
        self.remove_pending(xx)

    def on_initial_update(self, record):
        """Handle update of feval from initial design."""
        if record.status == 'completed':
            self.on_initial_completed(record)
        elif record.is_done:
            self.on_initial_aborted(record)

    def on_initial_completed(self, record):
        """Handle successful completion of feval from initial design."""

        if self.stopping_criterion is not None:
            if self.stopping_criterion(record.value):
                self.terminate = True

        self.num_evals += 1
        self.feval_pending -= 1
        self.init_pending -= 1
        record.worker_num_evals = self.num_evals
        record.feasible = True

        xx, fx = np.copy(record.params[0]), record.value
        self.X = np.vstack((self.X, np.asmatrix(xx)))
        self.fX = np.vstack((self.fX, fx))

        self.surrogate.add_points(xx, fx)
        self.remove_pending(xx)

        self.log_completion(record)
        self.fevals.append(record)

    def on_initial_aborted(self, record):
        """Handle aborted feval from initial design."""
        self.feval_budget += 1
        self.feval_pending -= 1
        self.init_pending -= 1
        xx = record.params[0]
        self.batch_queue.append(xx)
        self.remove_pending(xx)

    # == Processing in adaptive phase ==

    def adapt_proposal(self):
        """Propose a point from the batch_queue."""
        proposal = self.make_proposal(self.batch_queue.pop())
        proposal.add_callback(self.on_adapt_proposal)
        return proposal

    def on_adapt_proposal(self, proposal):
        """Handle accept/reject of proposal from sampling phase."""
        if proposal.accepted:
            self.on_adapt_accept(proposal)
        else:
            self.on_adapt_reject(proposal)

    def on_adapt_accept(self, proposal):
        """Handle accepted proposal from sampling phase."""
        self.accepted_count += 1
        proposal.record.add_callback(self.on_adapt_update)

    def on_adapt_reject(self, proposal):
        """Handle rejected proposal from sampling phase."""
        self.rejected_count += 1
        self.feval_budget += 1
        self.feval_pending -= 1
        xx = np.copy(proposal.args[0])
        self.remove_pending(xx)
        if not self.asynchronous:  # Add back to the queue in batch synchronous case
            self.batch_queue.append(xx)
            return self.adapt_proposal()

    def on_adapt_update(self, record):
        """Handle update of feval from sampling phase."""
        if record.status == 'completed':
            self.on_adapt_completed(record)
        elif record.is_done:
            self.on_adapt_aborted(record)

    def on_adapt_completed(self, record):
        """Handle completion of feval from sampling phase."""

        if self.stopping_criterion is not None:
            if self.stopping_criterion(record.value):
                self.terminate = True

        self.num_evals += 1
        self.feval_pending -= 1
        record.worker_num_evals = self.num_evals
        record.feasible = True

        xx, fx = np.copy(record.params[0]), record.value
        self.X = np.vstack((self.X, np.asmatrix(xx)))
        self.fX = np.vstack((self.fX, fx))
        self.surrogate.add_points(xx, fx)
        self.remove_pending(xx)

        self.log_completion(record)
        self.fevals.append(record)

    def on_adapt_aborted(self, record):
        """Handle aborted feval from sampling phase."""
        self.feval_budget += 1
        self.feval_pending -= 1
        xx =  np.copy(record.params[0])
        self.remove_pending(xx)


class SRBFStrategy(SurrogateBaseStrategy):
    """Parallel asynchronous SRBF optimization strategy.

    In the asynchronous version of SRBF, workers are given function
    evaluations to start on as soon as they become available (unless
    the initial experiment design has been assigned but not completed).
    As evaluations are completed, different actions are taken depending
    on how recent they are.  A "fresh" value is one that was assigned
    since the last time the sampling radius was checked; an "old"
    value is one that was assigned before the last check of the sampling
    radius, but since the last restart; and an "ancient" value is one
    that was assigned before the last restart.  Only fresh values are
    used in adjusting the sampling radius.  Fresh or old values are
    used in determing the best point found since restart (used for
    the center point for sampling).  Any value can be incorporated into
    the response surface.  Sample points are chosen based on a merit
    function that depends not only on the response surface and the distance
    from any previous sample points, but also on the distance from any
    pending sample points.

    Once the budget of maxeval function evaluations have been assigned,
    no further evaluations are assigned to processors.  The code returns
    once all evaluations are completed.
    """

    def __init__(self, max_evals, opt_prob, exp_design=None, surrogate=None,
                 asynchronous=True, batch_size=None, stopping_criterion=None, 
                 extra=None, weights=None):
        """Initialize the asynchronous SRBF optimization.

        Args:
            data: Problem parameter data structure
            surrogate: Surrogate model object
            maxeval: Function evaluation budget
            design: Experimental design

        """

        self.fbest = np.inf      # Current best f

        self.dtol = 1e-3 * math.sqrt(opt_prob.dim)
        self.weights = [0.3, 0.5, 0.8, 0.95]
        self.next_weight = 0

        self.sampling_radius_min = 0.2 * (0.5 ** 6)
        self.sampling_radius_max = 0.2
        self.sampling_radius = 0.2

        if asynchronous:
            self.failtol = int(max(np.ceil(float(opt_prob.dim)), np.ceil(4.0)))
        else:
            self.failtol = int(max(np.ceil(float(opt_prob.dim) / float(batch_size)),
                                   np.ceil(4.0 / float(batch_size))))
        self.succtol = 3
        self.maxfailtol = 4 * self.failtol

        self.status = 0          # Status counter
        self.failcount = 0       # Failure counter

        self.record_queue = []  # Completed records that haven't been processed

        # Weights for merit function
        if weights is None:
            weights = [0.3, 0.5, 0.8, 0.95]
        self.weights = weights
        self.weight_ind = 0
        
        super().__init__(max_evals=max_evals, opt_prob=opt_prob,
            exp_design=exp_design, surrogate=surrogate, asynchronous=asynchronous, 
            batch_size=batch_size, stopping_criterion=stopping_criterion, extra=extra)

    def check_input(self):
        pass

    def on_adapt_completed(self, record):
        super().on_adapt_completed(record)
        if self.asynchronous:  # Add to queue and process immediately
            self.record_queue.append(record)
            self.adjust_step()
        elif not self.asynchronous: 
            self.record_queue.append(record)
            if self.feval_pending == 0:  # Only process if the entire batch is done
                self.adjust_step()

    def get_weights(self, num_pts):
        """Generate the nextw weights."""
        weights = []
        for _ in range(num_pts):
            weights.append(self.weights[self.weight_ind])
            self.weight_ind = (self.weight_ind + 1) % len(self.weights)
        return weights

    def generate_evals(self, num_pts):
        """Generate the next adaptive sample point."""
        weights = self.get_weights(num_pts=num_pts)
        new_points = make_srbf(
            opt_prob=self.opt_prob, npts=num_pts, surrogate=self.surrogate, X=self.X, 
            fX=self.fX, Xpend=self.Xpend, sampling_radius=self.sampling_radius, 
            weights=weights)
        for x in new_points:
            self.batch_queue.append(np.copy(np.ravel(x)))

    def adjust_step(self):
        """Adjust the sampling radius sigma.

        After succtol successful steps, we cut the sampling radius;
        after failtol failed steps, we double the sampling radius.
        """

        # Check if we succeeded at significant improvement
        fbest_new = min([record.value for record in self.record_queue])
        if np.isinf(self.fbest) or fbest_new < self.fbest - 1e-3*math.fabs(self.fbest):  # Improvement
            self.fbest = fbest_new
            self.status = max(1, self.status + 1)
            self.failcount = 0
        else:
            self.status = min(-1, self.status - 1)  # No improvement
            self.failcount += 1

        # Check if step needs adjusting
        if self.status <= -self.failtol:
            self.status = 0
            self.sampling_radius /= 2
            logger.info("Reducing sampling radius")
        if self.status >= self.succtol:
            self.status = 0
            self.sampling_radius = min([2.0 * self.sampling_radius, self.sampling_radius_max])
            logger.info("Increasing sampling radius")

        # Check if we want to terminate
        if self.failcount >= self.maxfailtol or self.sampling_radius <= self.sampling_radius_min:
            self.terminate = True

        # Empty the queue
        self.record_queue = []


class DYCORSStrategy(SurrogateBaseStrategy):
    pass
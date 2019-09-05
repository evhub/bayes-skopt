import emcee as mc
import numpy as np
from contextlib import contextmanager, nullcontext
from scipy.linalg import cholesky, cho_solve, solve_triangular
from sklearn.utils import check_random_state
from skopt.learning import GaussianProcessRegressor
from skopt.learning.gaussian_process.kernels import WhiteKernel
from skopt.learning.gaussian_process.gpr import _param_for_white_kernel_in_Sum

from .utils import geometric_median


class BayesGPR(GaussianProcessRegressor):
    def __init__(
        self,
        kernel=None,
        alpha=1e-10,
        optimizer="fmin_l_bfgs_b",
        n_restarts_optimizer=0,
        normalize_y=False,
        copy_X_train=True,
        random_state=None,
        noise="gaussian",
    ):
        super().__init__(kernel, alpha, optimizer, n_restarts_optimizer, normalize_y, copy_X_train, random_state, noise)
        self._sampler = None
        self.chain = None

    @property
    def theta(self):
        if self.kernel_ is not None:
            with np.errstate(divide="ignore"):
                return np.copy(self.kernel_.theta)
        return None

    @theta.setter
    def theta(self, theta):
        self.kernel_.theta = theta
        K = self.kernel_(self.X_train_)
        try:
            self.L_ = cholesky(K, lower=True)
            L_inv = solve_triangular(self.L_.T, np.eye(self.L_.shape[0]))
            self.K_inv_ = L_inv.dot(L_inv.T)
        except np.linalg.LinAlgError as exc:
            exc.args = (
                "The kernel, %s, is not returning a "
                "positive definite matrix. Try gradually "
                "increasing the 'alpha' parameter of your "
                "GaussianProcessRegressor estimator." % self.kernel_,
            ) + exc.args
            raise
        self.alpha_ = cho_solve((self.L_, True), self.y_train_)

    @contextmanager
    def noise_set_to_zero(self):
        current_theta = self.theta
        # Saving the old kernel inverse in order to avoid recomputing it
        current_K_inv = np.copy(self.K_inv_)
        try:
            # Now we set the noise to 0, but do NOT recalculate the alphas!:
            white_present, white_param = _param_for_white_kernel_in_Sum(self.kernel_)
            self.kernel_.set_params(**{white_param: WhiteKernel(noise_level=0.0)})
            # Precompute arrays needed at prediction
            L_inv = solve_triangular(self.L_.T, np.eye(self.L_.shape[0]))
            self.K_inv_ = L_inv.dot(L_inv.T)
            yield self
        finally:
            self.kernel_.theta = current_theta
            self.K_inv_ = current_K_inv

    def fit(
        self,
        X,
        y,
        n_threads=1,
        n_desired_samples=100,
        n_burnin=10,
        n_walkers_per_thread=100,
        progress=True,
        priors=None,
        position=None,
        **kwargs
    ):
        super().fit(X, y)

        def log_prob_fn(x, gp=self):
            lp = 0
            for prior, val in zip(priors, x):
                lp += prior(val)
            lp = lp + gp.log_marginal_likelihood(theta=x)
            if not np.isfinite(lp):
                return -np.inf
            return lp

        n_dim = len(self.theta)
        n_walkers = n_threads * n_walkers_per_thread
        n_samples = np.ceil(n_desired_samples / n_walkers) + n_burnin
        pos = None
        if position is not None:
            pos = position
        # elif backup_file is not None:
        #     try:
        #         with open(backup_file, 'rb') as f:
        #             pos = np.load(f)
        #     except FileNotFoundError:
        #         pass
        if pos is None:
            theta = self.theta
            theta[np.isinf(theta)] = np.log(self.noise_)
            pos = [theta + 1e-2 * np.random.randn(n_dim) for _ in range(n_walkers)]
        self._sampler = mc.EnsembleSampler(
            nwalkers=n_walkers, ndim=n_dim, log_prob_fn=log_prob_fn, threads=n_threads, **kwargs
        )
        pos, prob, state = self._sampler.run_mcmc(pos, n_samples, progress=progress)
        # if backup_file is not None:
        #     with open(backup_file, "wb") as f:
        #         np.save(f, pos)
        self.chain = self._sampler.chain[:, n_burnin:, :].reshape(-1, n_dim)
        self.theta = geometric_median(self.chain)

    #def predict(self, X, return_std=False, return_cov=False, return_mean_grad=False, return_std_grad=False):
    #    return super().predict(X, return_std, return_cov, return_mean_grad, return_std_grad)

    def sample_y(self, X, sample_mean=False, noise=False, n_samples=1, random_state=0):
        if sample_mean:
            return super().sample_y(X, n_samples, random_state)
        if noise:
            cm = self.noise_set_to_zero()
        else:
            cm = nullcontext(self)
        rng = check_random_state(random_state)
        ind = rng.choice(len(self.chain), size=n_samples, replace=True)
        current_theta = self.theta
        current_K_inv = np.copy(self.K_inv_)
        result = np.empty((X.shape[0], n_samples))
        for i, j in enumerate(ind):
            theta = self.chain[j]
            self.theta = theta
            with cm:
                result[i] = super().sample_y(X, n_samples, random_state)
        self.kernel_.theta = current_theta
        self.K_inv_ = current_K_inv
        return result

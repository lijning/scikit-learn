# Author: Alexandre Gramfort <alexandre.gramfort@inria.fr>
#         Fabian Pedregosa <fabian.pedregosa@inria.fr>
#         Olivier Grisel <olivier.grisel@ensta.org>
#         Vincent Michel <vincent.michel@inria.fr>
#
# License: BSD Style.


"""
Generalized Linear models.
"""

import warnings

import numpy as np
import scipy.linalg 
import scipy.sparse as sp # needed by LeastAngleRegression

from . import cd_fast
from .utils.extmath import fast_logdet, density
from .cross_val import KFold
from ._minilearn import lars_fit_wrap
from .base_estimator import BaseEstimator

###
### TODO: intercept for all models
### We should define a common function to center data instead of
### repeating the same code inside each fit method.
###
### Also, bayesian_ridge_regression and bayesian_regression_ard
### should be squashed into its respective objects.
###

class LinearModel(BaseEstimator):
    """Base class for Linear Models"""

    def __init__(self, coef=None):
        # weights of the model (can be lazily initialized by the
        # ``fit`` method)
       # TODO: I believe this is not really much used
        self.coef_ = coef

    def predict(self, X):
        """
        Predict using the linear model

        Parameters
        ----------
        X : numpy array of shape [nsamples,nfeatures]

        Returns
        -------
        C : array, shape = [nsample]
            Returns predicted values.
        """
        X = np.asanyarray(X)
        return np.dot(X, self.coef_) + self.intercept_

    def _explained_variance(self, X, Y):
        """Compute explained variance a.k.a. r^2"""
        ## TODO: this should have a tests.
        return 1 - np.linalg.norm(Y - self.predict(X))**2 \
                         / np.linalg.norm(Y)**2


class LinearRegression(LinearModel):
    """
    Ordinary least squares Linear Regression.

    Attributes
    ----------
    `coef_` : array
        Estimated coefficients for the linear regression problem.

    `intercept_` : array
        Independent term in the linear model.

    Notes
    -----
    From the implementation point of view, this is just plain Ordinary
    Least Squares (numpy.linalg.lstsq) wrapped as a predictor object.

    """

    def fit(self, X, Y, intercept=True):
        """
        Fit linear model.

        Parameters
        ----------
        X : numpy array of shape [nsamples,nfeatures]
            Training data
        Y : numpy array of shape [nsamples]
            Target values
        intercept : boolen
            wether to calculate the intercept for this model. If set
            to false, no intercept will be used in calculations
            (e.g. data is expected to be already centered).

        Returns
        -------
        self : returns an instance of self.
        """
        X = np.asanyarray( X )
        Y = np.asanyarray( Y )

        if intercept:
            # augmented X array to store the intercept
            X = np.c_[X, np.ones(X.shape[0])]
        self.coef_, self.residues_, self.rank_, self.singular_ = \
                np.linalg.lstsq(X, Y)
        if intercept:
            self.intercept_ = self.coef_[-1]
            self.coef_ = self.coef_[:-1]
        else:
            self.intercept_ = np.zeros(self.coef_X.shape[1])
        return self


class Ridge(LinearModel):
    """
    Ridge regression.


    Parameters
    ----------
    alpha : float
        Small positive values of alpha improve the coditioning of the
        problem and reduce the variance of the estimates.

    Examples
    --------
    >>> import numpy as np
    >>> nsamples, nfeatures = 10, 5
    >>> np.random.seed(0)
    >>> Y = np.random.randn(nsamples)
    >>> X = np.random.randn(nsamples, nfeatures)
    >>> clf = Ridge(alpha=1.0)
    >>> clf.fit(X, Y) #doctest: +ELLIPSIS
    <scikits.learn.glm.Ridge object at 0x...>
    """

    def __init__(self, alpha=1.0):
        self.alpha = alpha

    def fit(self, X, Y, intercept=True):
        """
        Fit Ridge regression model

        Parameters
        ----------
        X : numpy array of shape [nsamples,nfeatures]
            Training data
        Y : numpy array of shape [nsamples]
            Target values

        Returns
        -------
        self : returns an instance of self.
        """
        nsamples, nfeatures = X.shape

        self._intercept = intercept
        if self._intercept:
            self._xmean = X.mean(axis=0)
            self._ymean = Y.mean(axis=0)
            X = X - self._xmean
            Y = Y - self._ymean
        else:
            self._xmean = 0.
            self._ymean = 0.


        if nsamples > nfeatures:
            # w = inv(X^t X + alpha*Id) * X.T y
            self.coef_ = scipy.linalg.solve(
                np.dot(X.T, X) + self.alpha * np.eye(nfeatures),
                np.dot(X.T, Y))
        else:
            # w = X.T * inv(X X^t + alpha*Id) y
            self.coef_ = np.dot(X.T, scipy.linalg.solve(
                np.dot(X, X.T) + self.alpha * np.eye(nsamples), Y))

        self.intercept_ = self._ymean - np.dot(self._xmean, self.coef_)
        return self


class BayesianRidge(LinearModel):
    """
    Encapsulate various bayesian regression algorithms
    """

    def __init__(self, ll_bool=False, step_th=300, th_w=1.e-12):
        self.ll_bool = ll_bool
        self.step_th = step_th
        self.th_w = th_w

    def fit(self, X, Y, intercept=True):
        """
        Parameters
        ----------
        X : numpy array of shape [nsamples,nfeatures]
            Training data
        Y : numpy array of shape [nsamples]
            Target values

        Returns
        -------
        self : returns an instance of self.
        """
        X = np.asanyarray(X, dtype=np.float)
        Y = np.asanyarray(Y, dtype=np.float)

        self._intercept = intercept
        if self._intercept:
            self._xmean = X.mean(axis=0)
            self._ymean = Y.mean(axis=0)
            X = X - self._xmean
            Y = Y - self._ymean
        else:
            self._xmean = 0.
            self._ymean = 0.

        # todo, shouldn't most of these have trailing underscores ?
        self.coef_, self.alpha, self.beta, self.sigma, self.log_likelihood = \
            bayesian_ridge_regression(X, Y, self.step_th, self.th_w, self.ll_bool)

        self.intercept_ = self._ymean - np.dot(self._xmean, self.coef_)

        return self


class ARDRegression(LinearModel):
    """
    Encapsulate various bayesian regression algorithms
    """
    # TODO: add intercept

    def __init__(self, ll_bool=False, step_th=300, th_w=1.e-12,\
        alpha_th=1.e+16):
        self.ll_bool = ll_bool
        self.step_th = step_th
        self.th_w = th_w
        self.alpha_th = alpha_th

    def fit(self, X, Y):
        X = np.asanyarray(X, dtype=np.float)
        Y = np.asanyarray(Y, dtype=np.float)
        self.w ,self.alpha ,self.beta ,self.sigma ,self.log_likelihood = \
            bayesian_regression_ard(X, Y, self.step_th, self.th_w,\
            self.alpha_th, self.ll_bool)
        return self

    def predict(self, T):
        return np.dot(T, self.w)



### helper methods
### we should homogeneize this

def bayesian_ridge_regression(X , Y, step_th=300, th_w = 1.e-12, ll_bool=False):
    """
    Bayesian ridge regression. Optimize the regularization parameters alpha
    (precision of the weights) and beta (precision of the noise) within a simple
    bayesian framework (MAP).

    Parameters
    ----------
    X : numpy array of shape (length,features)
    data
    Y : numpy array of shape (length)
    target
    step_th : int (defaut is 300)
          Stop the algorithm after a given number of steps.
    th_w : float (defaut is 1.e-12)
       Stop the algorithm if w has converged.
    ll_bool  : boolean (default is False).
           If True, compute the log-likelihood at each step of the model.

    Returns
    -------
    w : numpy array of shape (nb_features)
         mean of the weights distribution.
    alpha : float
       precision of the weights.
    beta : float
       precision of the noise.
    sigma : numpy array of shape (nb_features,nb_features)
        variance-covariance matrix of the weights
    log_likelihood : list of float of size steps.
             Compute (if asked) the log-likelihood of the model.

    Examples
    --------
    >>> X = np.array([[1], [2]])
    >>> Y = np.array([1, 2])
    >>> w = bayesian_ridge_regression(X,Y)

    Notes
    -----
    See Bishop p 167-169 for more details.
    """

    beta = 1./np.var(Y)
    alpha = 1.0

    log_likelihood = []
    has_converged = False
    gram = np.dot(X.T, X)
    ones = np.eye(gram.shape[1])
    sigma = scipy.linalg.pinv(alpha*ones + beta*gram)
    w = np.dot(beta*sigma,np.dot(X.T,Y))
    old_w = np.copy(w)
    eigen = np.real(scipy.linalg.eigvals(gram.T))
    while not has_converged and step_th:

        ### Update Parameters
        # alpha
        lmbd_ = np.dot(beta, eigen)
        gamma_ = (lmbd_/(alpha + lmbd_)).sum()
        alpha = gamma_/np.dot(w.T, w)

        # beta
        residual_ = (Y - np.dot(X, w))**2
        beta = (X.shape[0]-gamma_) / residual_.sum()

        ### Compute mu and sigma
        sigma = scipy.linalg.pinv(alpha*ones + beta*gram)
        w = np.dot(beta*sigma,np.dot(X.T,Y))
        step_th -= 1

        # convergence : compare w
        has_converged =  (np.sum(np.abs(w-old_w))<th_w)
        old_w = w

    ### Compute the log likelihood
    if ll_bool:
        residual_ = (Y - np.dot(X, w))**2
        ll = 0.5*X.shape[1]*np.log(alpha) + 0.5*X.shape[0]*np.log(beta)
        ll -= (0.5*beta*residual_.sum()+ 0.5*alpha*np.dot(w.T,w))
        ll -= fast_logdet(alpha*ones + beta*gram)
        ll -= X.shape[0]*np.log(2*np.pi)
        log_likelihood.append(ll)

    return w, alpha, beta, sigma, log_likelihood



def bayesian_regression_ard(X, Y, step_th=300, th_w=1.e-12, \
                alpha_th=1.e+16, ll_bool=False):
    """
    Bayesian ard-based regression. Optimize the regularization parameters alpha
    (vector of precisions of the weights) and beta (precision of the noise).


    Parameters
    ----------
    X : numpy array of shape (length,features)
    data
    Y : numpy array of shape (length)
    target
    step_th : int (defaut is 300)
          Stop the algorithm after a given number of steps.
    th_w : float (defaut is 1.e-12)
       Stop the algorithm if w has converged.
    alpha_th : number
           threshold on the alpha, to avoid divergence. Remove those features
       from the weights computation if is alpha > alpha_th  (default is
        1.e+16).
    ll_bool  : boolean (default is False).
           If True, compute the log-likelihood at each step of the model.

    Returns
    -------
    w : numpy array of shape (nb_features)
         mean of the weights distribution.
    alpha : numpy array of shape (nb_features)
       precision of the weights.
    beta : float
       precision of the noise.
    sigma : numpy array of shape (nb_features,nb_features)
        variance-covariance matrix of the weights
    log_likelihood : list of float of size steps.
             Compute (if asked) the log-likelihood of the model.

    Examples
    --------

    Notes
    -----
    See Bishop chapter 7.2. for more details.
    This should be resived. It is not efficient and I wonder if we
    can't use libsvm for this.
    """
    gram = np.dot(X.T, X)
    beta = 1./np.var(Y)
    alpha = np.ones(gram.shape[1])


    log_likelihood = None
    if ll_bool :
        log_likelihood = []
    has_converged = False
    ones = np.eye(gram.shape[1])
    sigma = scipy.linalg.pinv(alpha*ones + beta*gram)
    w = np.dot(beta*sigma,np.dot(X.T,Y))
    old_w = np.copy(w)
    keep_a  = np.ones(X.shape[1],dtype=bool)
    while not has_converged and step_th:

        # alpha
        gamma_ = 1 - alpha[keep_a]*np.diag(sigma)
        alpha[keep_a] = gamma_/w[keep_a]**2

        # beta
        residual_ = (Y - np.dot(X[:,keep_a], w[keep_a]))**2
        beta = (X.shape[0]-gamma_.sum()) / residual_.sum()

        ### Avoid divergence of the values by setting a maximum values of the
        ### alpha
        keep_a = alpha<alpha_th
        gram = np.dot(X.T[keep_a,:], X[:,keep_a])

        ### Compute mu and sigma
        ones = np.eye(gram.shape[1])
        sigma = scipy.linalg.pinv(alpha[keep_a]*ones+ beta*gram)
        w[keep_a] = np.dot(beta*sigma,np.dot(X.T[keep_a,:],Y))
        step_th -= 1

        # convergence : compare w
        has_converged =  (np.sum(np.abs(w-old_w))<th_w)
        old_w = w


    ### Compute the log likelihood
    if ll_bool :
        A_ = np.eye(X.shape[1])/alpha
        C_ = (1./beta)*np.eye(X.shape[0]) + np.dot(X,np.dot(A_,X.T))
        ll = X.shape[0]*np.log(2*np.pi)+fast_logdet(C_)
        ll += np.dot(Y.T,np.dot(scipy.linalg.pinv(C_),Y))
        log_likelihood.append(-0.5*ll)

    return w, alpha, beta, sigma, log_likelihood


class Lasso(LinearModel):
    """
    Linear Model trained with L1 prior as regularizer (a.k.a. the
    lasso).

    Parameters
    ----------
    alpha : float, optional
        Constant that multiplies the L1 term. Defaults to 1.0

    intercept : boolean
        whether to calculate the intercept for this model. If set
        to false, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    Attributes
    ----------
    `coef_` : array, shape = [nfeatures]
        parameter vector (w in the fomulation formula)

    `intercept_` : float
        independent term in decision function.

    Examples
    --------
    >>> from scikits.learn import glm
    >>> clf = glm.Lasso(alpha=0.1)
    >>> clf.fit([[0,0], [1, 1], [2, 2]], [0, 1, 2])
    Lasso Coordinate Descent
    >>> print clf.coef_
    [ 0.85  0.  ]
    >>> print clf.intercept_
    0.15

    Notes
    -----
    The algorithm used to fit the model is coordinate descent.
    """

     _params = {'alpha': [float, int], 'tol': float}

    def __init__(self, alpha=1.0, tol=1e-4, intercept=True):
        super(Lasso, self).__init__(alpha=alpha, tol=tol, intercept=intercept)

    def fit(self, X, Y, maxit=1000, **params):
        """
        Fit Lasso model.

        Parameters
        ----------
        X : numpy array of shape [nsamples,nfeatures]
            Training data

        Y : numpy array of shape [nsamples]
            Target values

        maxit : int

            maximum number of coordinate descent iterations used to
            fit the model. In case

        Returns
        -------
        self : returns an instance of self.
        """
        self._set_params(self, **params)

        X = np.asanyarray(X, dtype=np.float64)
        Y = np.asanyarray(Y, dtype=np.float64)

        self._intercept = intercept
        if self._intercept:
            self._xmean = X.mean(axis=0)
            self._ymean = Y.mean(axis=0)
            X = X - self._xmean
            Y = Y - self._ymean
        else:
            self._xmean = np.zeros(X.shape[1])
            self._ymean = np.zeros(X.shape[0])

        nsamples = X.shape[0]
        alpha = self.alpha * nsamples

        if self.coef_ is None:
            self.coef_ = np.zeros(X.shape[1], dtype=np.float64)

        X = np.asfortranarray(X) # make data contiguous in memory
        self.coef_, self.dual_gap_, self.eps_ = \
                    cd_fast.lasso_coordinate_descent(self.coef_,
                    alpha, X, Y, maxit, 10, self.tol)

        self.intercept_ = self._ymean - np.dot(self._xmean, self.coef_)

        if self.dual_gap_ > self.eps_:
            warnings.warn('Objective did not converge, you might want '
                                'to increase the number of interations')

        self.explained_variance_ = self._explained_variance(X, Y)

        # return self for chaining fit and predict calls
        return self

    def __str__(self):
        if self.coef_ is not None:
            n_non_zeros = (np.abs(self.coef_) != 0).sum()
            return ("%s with %d non-zero coefficients (%.2f%%)\n" + \
                    " * Regularisation parameter = %.7f\n" +
                    " * Explained Variance = %.7f\n") % \
                    (self.__class__.__name__, n_non_zeros,
                     n_non_zeros / float(len(self.coef_)) * 100,
                     self.alpha, self.explained_variance_)
        else:
            return ("%s\n" + \
                    " * Regularisation parameter = %.7f\n" +\
                    " * No fit") % \
                    (self.__class__.__name__, self.alpha)


class ElasticNet(Lasso):
    """Linear Model trained with L1 and L2 prior as regularizer

    rho=1 is the lasso penalty. Currently, rho <= 0.01 is not
    reliable, unless you supply your own sequence of alpha.

    Parameters
    ----------
    alpha : float
        Constant that multiplies the L1 term. Defaults to 1.0
    rho : float
        The ElasticNet mixing parameter, with 0 < rho <= 1.
    """

    def __init__(self, alpha=1.0, rho=0.5, coef=None, tol=1e-4):
        self.coef_ = coef
        self.alpha = alpha
        self.rho = rho
        self.tol = tol

    def fit(self, X, Y, intercept=True, maxit=1000):
        """Fit Elastic Net model with coordinate descent"""
        X = np.asanyarray(X, dtype=np.float64)
        Y = np.asanyarray(Y, dtype=np.float64)

        self._intercept = intercept
        if self._intercept:
            self._xmean = X.mean(axis=0)
            self._ymean = Y.mean(axis=0)
            X = X - self._xmean
            Y = Y - self._ymean
        else:
            self._xmean = np.zeros(X.shape[1])
            self._ymean = np.zeros(X.shape[0])

        if self.coef_ is None:
            self.coef_ = np.zeros(X.shape[1], dtype=np.float64)

        nsamples = X.shape[0]
        alpha = self.alpha * self.rho * nsamples
        beta = self.alpha * (1.0 - self.rho) * nsamples

        X = np.asfortranarray(X) # make data contiguous in memory

        self.coef_, self.dual_gap_, self.eps_ = \
                cd_fast.enet_coordinate_descent(self.coef_, alpha, beta, X, Y,
                                        maxit, 10, self.tol)

        self.intercept_ = self._ymean - np.dot(self._xmean, self.coef_)

        if self.dual_gap_ > self.eps_:
            warnings.warn('Objective did not converge, you might want to increase the number of interations')

        self.explained_variance_ = self._explained_variance(X, Y)


        # return self for chaining fit and predict calls
        return self


################################################################################
# Classes to store linear models along a regularization path 
################################################################################

def lasso_path(X, y, eps=1e-3, n_alphas=100, alphas=None,
               verbose=False, **fit_kwargs):
    """
    Compute Lasso path with coordinate descent

    Parameters
    ----------
    X : numpy array of shape [nsamples,nfeatures]
        Training data

    Y : numpy array of shape [nsamples]
        Target values

    eps : float, optional
        Length of the path. eps=1e-3 means that
        alpha_min / alpha_max = 1e-3

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : numpy array, optional
        List of alphas where to compute the models.
        If None alphas are set automatically

    fit_kwargs : kwargs, optional
        keyword arguments passed to the Lasso fit method

    Returns
    -------
    models : a list of models along the regularization path

    Notes
    -----
    See examples/plot_lasso_coordinate_descent_path.py for an example.
    """
    nsamples = X.shape[0]
    if alphas is None:
        alpha_max = np.abs(np.dot(X.T, y)).max() / nsamples
        alphas = np.linspace(np.log(alpha_max), np.log(eps * alpha_max), n_alphas)
        alphas = np.exp(alphas)
    else:
        alphas = np.sort(alphas)[::-1] # make sure alphas are properly ordered
    coef = None # init coef_
    models = []
    for alpha in alphas:
        model = Lasso(coef=coef, alpha=alpha)
        model.fit(X, y, **fit_kwargs)
        if verbose: print model
        coef = model.coef_.copy()
        models.append(model)
    return models

def enet_path(X, y, rho=0.5, eps=1e-3, n_alphas=100, alphas=None,
              verbose=False, **fit_kwargs):

    """Compute Elastic-Net path with coordinate descent

    Parameters
    ----------
    X : numpy array of shape [nsamples,nfeatures]
        Training data

    Y : numpy array of shape [nsamples]
        Target values

    eps : float
        Length of the path. eps=1e-3 means that
        alpha_min / alpha_max = 1e-3

    n_alphas : int
        Number of alphas along the regularization path

    alphas : numpy array
        List of alphas where to compute the models.
        If None alphas are set automatically

    fit_kwargs : kwargs
        keyword arguments passed to the ElasticNet fit method

    Returns
    -------
    models : a list of models along the regularization path

    Notes
    -----
    See examples/plot_lasso_coordinate_descent_path.py for an example.
    """
    nsamples = X.shape[0]
    if alphas is None:
        alpha_max = np.abs(np.dot(X.T, y)).max() / (nsamples*rho)
        alphas = np.linspace(np.log(alpha_max), np.log(eps * alpha_max), n_alphas)
        alphas = np.exp(alphas)
    else:
        alphas = np.sort(alphas)[::-1] # make sure alphas are properly ordered
    coef = None # init coef_
    models = []
    for alpha in alphas:
        model = ElasticNet(coef=coef, alpha=alpha, rho=rho)
        model.fit(X, y, **fit_kwargs)
        if verbose: print model
        coef = model.coef_.copy()
        models.append(model)
    return models


def optimized_lasso(X, y, cv=None, n_alphas=100, alphas=None,
                                eps=1e-3, **fit_kwargs):
    """Compute an optimized Lasso model

    Parameters
    ----------
    X : numpy array of shape [nsamples,nfeatures]
        Training data

    Y : numpy array of shape [nsamples]
        Target values

    rho : float, optional
        float between 0 and 1 passed to ElasticNet (scaling between
        l1 and l2 penalties)

    cv : cross-validation generator, optional
         If None, KFold will be used.

    eps : float, optional
        Length of the path. eps=1e-3 means that
        alpha_min / alpha_max = 1e-3.

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : numpy array, optional
        List of alphas where to compute the models.
        If None alphas are set automatically

    fit_kwargs : kwargs
        keyword arguments passed to the Lasso fit method

    Returns
    -------
    model : a Lasso instance model

    Notes
    -----
    See examples/lasso_path_with_crossvalidation.py for an example.
    """
    # Start to compute path on full data
    models = lasso_path(X, y, eps=eps, n_alphas=n_alphas, alphas=alphas,
                                **fit_kwargs)

    n_samples = y.size
    # init cross-validation generator
    cv = cv if cv else KFold(n_samples, 5)

    alphas = [model.alpha for model in models]
    n_alphas = len(alphas)
    # Compute path for all folds and compute MSE to get the best alpha
    mse_alphas = np.zeros(n_alphas)
    for train, test in cv:
        models_train = lasso_path(X[train], y[train], eps, n_alphas,
                                    alphas=alphas, **fit_kwargs)
        for i_alpha, model in enumerate(models_train):
            y_ = model.predict(X[test])
            mse_alphas[i_alpha] += ((y_ - y[test]) ** 2).mean()

    i_best_alpha = np.argmin(mse_alphas)
    return models[i_best_alpha]


def optimized_enet(X, y, rho=0.5, cv=None, n_alphas=100, alphas=None,
                                 eps=1e-3, **fit_kwargs):
    """Returns an ElasticNet model that is optimized in the sense of
    cross validation.

    Parameters
    ----------
    X : numpy array of shape [nsamples,nfeatures]
        Training data

    Y : numpy array of shape [nsamples]
        Target values

    rho : float, optional
        float between 0 and 1 passed to ElasticNet (scaling between
        l1 and l2 penalties)

    cv : cross-validation generator, optional
         If None, KFold will be used.

    eps : float, optional
        Length of the path. eps=1e-3 means that
        alpha_min / alpha_max = 1e-3.

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : numpy array, optional
        List of alphas where to compute the models.
        If None alphas are set automatically

    fit_kwargs : kwargs
        keyword arguments passed to the ElasticNet fit method

    Returns
    -------
    model : a Lasso instance model

    Notes
    -----
    See examples/lasso_path_with_crossvalidation.py for an example.
    """
    # Start to compute path on full data
    models = enet_path(X, y, rho=rho, eps=eps, n_alphas=n_alphas,
                                alphas=alphas, **fit_kwargs)

    n_samples = y.size
    # init cross-validation generator
    cv = cv if cv else KFold(n_samples, 5)

    alphas = [model.alpha for model in models]
    n_alphas = len(alphas)
    # Compute path for all folds and compute MSE to get the best alpha
    mse_alphas = np.zeros(n_alphas)
    for train, test in cv:
        models_train = enet_path(X[train], y[train], rho=rho,
                                    alphas=alphas, eps=eps, n_alphas=n_alphas,
                                    **fit_kwargs)
        for i_alpha, model in enumerate(models_train):
            y_ = model.predict(X[test])
            mse_alphas[i_alpha] += ((y_ - y[test]) ** 2).mean()

    i_best_alpha = np.argmin(mse_alphas)
    return models[i_best_alpha]


class LinearModelCV(LinearModel):
    """Base class for iterative model fitting along a regularization path"""

    _params = {'eps':float, 'n_alphas':int, 'alphas':list}

    def __init__(self, eps=1e-3, n_alphas=100, alphas=None, cv=None):
        super(Lasso, self).__init__(eps=eps, n_alphas=n_alphas, alphas=alphas,
                                    cv=cv)

    def fit(self, X, y, **fit_params):
        """Fit linear model with coordinate descent along decreasing alphas
        """
        X = np.asanyarray(X, dtype=np.float64)
        y = np.asanyarray(y, dtype=np.float64)

        self.path_ = []
        n_samples = X.shape[0]

        # model = self.path(X, y, cv=cv, eps=self.eps, n_alphas=self.n_alphas,
        #                             **fit_kwargs)

        # Start to compute path on full data
        models = self.path(X, y, fit_params=fit_params, **self._get_params())

        n_samples = y.size
        # init cross-validation generator
        cv = cv if cv else KFold(n_samples, 5)

        alphas = [model.alpha for model in models]
        n_alphas = len(alphas)
        # Compute path for all folds and compute MSE to get the best alpha
        mse_alphas = np.zeros(n_alphas)
        for train, test in cv:
            models_train = self.path(X[train], y[train], rho=rho,
                                        alphas=alphas, eps=eps, n_alphas=n_alphas,
                                        **fit_params)
            for i_alpha, model in enumerate(models_train):
                y_ = model.predict(X[test])
                mse_alphas[i_alpha] += ((y_ - y[test]) ** 2).mean()

        i_best_alpha = np.argmin(mse_alphas)
        return models[i_best_alpha]

        self.__dict__.update(model.__dict__)
        return self


class LassoCV(LinearModelCV):
    """Lasso linear model with iterative fitting along a regularization path

    The best model is then sselected by cross-validation.
    """

    path = staticmethod(lasso_path)


class ElasticNetCV(LinearModelCV):
    """Elastic Net model with iterative fitting along a regularization path"""

    path = staticmethod(optimized_enet)

    def __init__(self, rho=0.5, **kwargs):
        super(ElasticNetCV, self).__init__(**kwargs)
        self.rho = rho


class LeastAngleRegression (LinearModel):
    """
    Least Angle Regression using the LARS algorithm.

    Least Angle Regression

    Attributes
    ----------
    `coef_` : array, shape = [n_features]
        parameter vector (w in the fomulation formula)

    `intercept_` : float
        independent term in decision function.

    `coef_path_` : array, shape = [max_features + 1, n_features]
         Full coeffients path.

    Notes
    -----
    predict does only work correctly in the case of normalized
    predictors.

    See also
    --------
    scikits.learn.glm.Lasso

    """

    def __init__(self):
        self.alphas_ = np.empty(0, dtype=np.float64)
        self._chol   = np.empty(0, dtype=np.float64)
        self.beta_    = np.empty(0, dtype=np.float64)

    def fit (self, X, Y, intercept=True, max_features=None, normalize=True):
        """
        Fit the model according to data X, Y.

        Parameters
        ----------
        X : numpy array of shape [nsamples,nfeatures]
            Training data

        Y : numpy array of shape [nsamples]
            Target values

        intercept : boolean, optional
            wether to calculate the intercept for this model. If set
            to false, no intercept will be used in calculations
            (e.g. data is expected to be already centered).

        max_features : int, optional
            number of features to get into the model. The iterative
            will stop just before the `max_features` variable enters
            in the active set. If not specified, min(N, p) - 1
            will be used.

        normalize : boolean
            whether to normalize (make all non-zero columns have mean
            0 and norm 1).
        """
        ## TODO: resize (not create) arrays, check shape,
        ##    add a real intercept

        X  = np.asanyarray(X, dtype=np.float64, order='C')
        _Y = np.asanyarray(Y, dtype=np.float64, order='C')

        if Y is _Y: Y = _Y.copy()
        else: Y = _Y

        if max_features is None:
            max_features = min(*X.shape)-1

        sum_k = max_features * (max_features + 1) /2
        self.alphas_.resize(max_features + 1)
        self._chol.resize(sum_k)
        self.beta_.resize(sum_k)
        coef_row = np.zeros(sum_k, dtype=np.int32)
        coef_col = np.zeros(sum_k, dtype=np.int32)


        if normalize:
            # will only normalize non-zero columns
            self._xmean = X.mean(0)
            self._ymean = Y.mean(0)
            X = X - self._xmean
            Y = Y - self._ymean
            self._norms = np.apply_along_axis (np.linalg.norm, 0, X)
            nonzeros = np.flatnonzero(self._norms)
            X[:, nonzeros] /= self._norms[nonzeros]
        else:
            self._xmean = 0.
            self._ymean = 0.

        lars_fit_wrap(0, X, Y, self.beta_, self.alphas_, coef_row,
                      coef_col, self._chol, max_features)

        self.coef_path_ = sp.coo_matrix((self.beta_,
                                        (coef_row, coef_col)),
                                        shape=(X.shape[1], max_features+1)).todense()

        self.coef_ = np.ravel(self.coef_path_[:, max_features])

        if intercept:
            self.intercept_ = self._ymean
        else:
            self.intercept_ = 0.

        return self


    def predict(self, X, normalize=True):
        """
        Predict using the linear model.

        Parameters
        ----------
        X : numpy array of shape [nsamples,nfeatures]

        Returns
        -------
        C : array, shape = [nsample]
            Returns predicted values.
        """
        X = np.asanyarray(X, dtype=np.float64, order='C')
        if normalize:
            X -= self._xmean
            X /= self._norms
        return  np.dot(X, self.coef_) + self.intercept_



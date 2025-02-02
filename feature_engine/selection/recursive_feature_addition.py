from typing import List, Union

import pandas as pd
from sklearn.model_selection import cross_validate

from feature_engine.dataframe_checks import _is_dataframe
from feature_engine.selection.base_selector import BaseSelector, get_feature_importances
from feature_engine.validation import _return_tags
from feature_engine.variable_manipulation import (
    _check_input_parameter_variables,
    _find_or_check_numerical_variables,
)

Variables = Union[None, int, str, List[Union[str, int]]]


class RecursiveFeatureAddition(BaseSelector):
    """
    RecursiveFeatureAddition() selects features following a recursive addition process.

    The process is as follows:

    1. Train an estimator using all the features.

    2. Rank the features according to their importance derived from the estimator.

    3. Train an estimator with the most important feature and determine performance.

    4. Add the second most important feature and train a new estimator.

    5. Calculate the difference in performance between estimators.

    6. If the performance increases beyond the threshold, the feature is kept.

    7. Repeat steps 4-6 until all features have been evaluated.

    Model training and performance calculation are done with cross-validation.

    More details in the :ref:`User Guide <recursive_addition>`.

    Parameters
    ----------
    estimator: object
        A Scikit-learn estimator for regression or classification.
        The estimator must have either a `feature_importances` or `coef_` attribute
        after fitting.

    variables: str or list, default=None
        The list of variable to be evaluated. If None, the transformer will evaluate
        all numerical features in the dataset.

    scoring: str, default='roc_auc'
        Desired metric to optimise the performance of the estimator. Comes from
        sklearn.metrics. See the model evaluation documentation for more options:
        https://scikit-learn.org/stable/modules/model_evaluation.html

    threshold: float, int, default = 0.01
        The value that defines if a feature will be kept or removed. Note that for
        metrics like roc-auc, r2_score and accuracy, the thresholds will be floats
        between 0 and 1. For metrics like the mean_square_error and the
        root_mean_square_error the threshold can be a big number.
        The threshold must be defined by the user. Bigger thresholds will select less
        features.

    cv: int, cross-validation generator or an iterable, default=3
        Determines the cross-validation splitting strategy. Possible inputs for cv are:

            - None, to use cross_validate's default 5-fold cross validation

            - int, to specify the number of folds in a (Stratified)KFold,

            - CV splitter
                - (https://scikit-learn.org/stable/glossary.html#term-CV-splitter)

            - An iterable yielding (train, test) splits as arrays of indices.

        For int/None inputs, if the estimator is a classifier and y is either binary or
        multiclass, StratifiedKFold is used. In all other cases, KFold is used. These
        splitters are instantiated with `shuffle=False` so the splits will be the same
        across calls. For more details check Scikit-learn's `cross_validate`'s
        documentation.

    Attributes
    ----------
    initial_model_performance_ :
        Performance of the model trained using the original dataset.

    feature_importances_ :
        Pandas Series with the feature importance (comes from step 2)

    performance_drifts_:
        Dictionary with the performance drift per examined feature (comes from step 5).

    features_to_drop_:
        List with the features to remove from the dataset.

    variables_:
        The variables that will be considered for the feature selection.

    n_features_in_:
        The number of features in the train set used in fit.


    Methods
    -------
    fit:
        Find the important features.
    transform:
         Reduce X to the selected features.
    fit_transform:
        Fit to data, then transform it.
    """

    def __init__(
        self,
        estimator,
        scoring: str = "roc_auc",
        cv=3,
        threshold: Union[int, float] = 0.01,
        variables: Variables = None,
    ):

        if not isinstance(threshold, (int, float)):
            raise ValueError("threshold can only be integer or float")

        self.variables = _check_input_parameter_variables(variables)
        self.estimator = estimator
        self.scoring = scoring
        self.threshold = threshold
        self.cv = cv

    def fit(self, X: pd.DataFrame, y: pd.Series):
        """
        Find the important features. Note that the selector trains various models at
        each round of selection, so it might take a while.

        Parameters
        ----------
        X: pandas dataframe of shape = [n_samples, n_features]
           The input dataframe

        y: array-like of shape (n_samples)
           Target variable. Required to train the estimator.
        """

        # check input dataframe
        X = _is_dataframe(X)

        # find numerical variables or check variables entered by user
        self.variables_ = _find_or_check_numerical_variables(X, self.variables)

        # train model with all features and cross-validation
        model = cross_validate(
            self.estimator,
            X[self.variables_],
            y,
            cv=self.cv,
            scoring=self.scoring,
            return_estimator=True,
        )

        # store initial model performance
        self.initial_model_performance_ = model["test_score"].mean()

        # Initialize a dataframe that will contain the list of the feature/coeff
        # importance for each cross validation fold
        feature_importances_cv = pd.DataFrame()

        # Populate the feature_importances_cv dataframe with columns containing
        # the feature importance values for each model returned by the cross
        # validation.
        # There are as many columns as folds.
        for m in model["estimator"]:

            feature_importances_cv[m] = get_feature_importances(m)

        # Add the variables as index to feature_importances_cv
        feature_importances_cv.index = self.variables_

        # Aggregate the feature importance returned in each fold
        self.feature_importances_ = feature_importances_cv.mean(axis=1)

        # Sort the feature importance values decreasingly
        self.feature_importances_.sort_values(ascending=False, inplace=True)

        # Extract most important feature from the ordered list of features
        first_most_important_feature = list(self.feature_importances_.index)[0]

        # Run baseline model using only the most important feature
        baseline_model = cross_validate(
            self.estimator,
            X[first_most_important_feature].to_frame(),
            y,
            cv=self.cv,
            scoring=self.scoring,
            return_estimator=True,
        )

        # Save baseline model performance
        baseline_model_performance = baseline_model["test_score"].mean()

        # list to collect selected features
        # It is initialized with the most important feature
        _selected_features = [first_most_important_feature]

        # dict to collect features and their performance_drift
        # It is initialized with the performance drift of
        # the most important feature
        self.performance_drifts_ = {first_most_important_feature: 0}

        # loop over the ordered list of features by feature importance starting
        # from the second element in the list.
        for feature in list(self.feature_importances_.index)[1:]:

            # Add feature and train new model
            model_tmp = cross_validate(
                self.estimator,
                X[_selected_features + [feature]],
                y,
                cv=self.cv,
                scoring=self.scoring,
                return_estimator=True,
            )

            # assign new model performance
            model_tmp_performance = model_tmp["test_score"].mean()

            # Calculate performance drift
            performance_drift = model_tmp_performance - baseline_model_performance

            # Save feature and performance drift
            self.performance_drifts_[feature] = performance_drift

            # If new performance model is
            if performance_drift > self.threshold:

                # add feature to the list of selected features
                _selected_features.append(feature)

                # Update new baseline model performance
                baseline_model_performance = model_tmp_performance

        self.features_to_drop_ = [
            f for f in self.variables_ if f not in _selected_features
        ]

        self.n_features_in_ = X.shape[1]

        return self

    # Ugly work around to import the docstring for Sphinx, otherwise not necessary
    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        X = super().transform(X)

        return X

    transform.__doc__ = BaseSelector.transform.__doc__

    def _more_tags(self):
        tags_dict = _return_tags()
        # add additional test that fails
        tags_dict["_xfail_checks"]["check_estimators_nan_inf"] = "transformer allows NA"
        tags_dict["_xfail_checks"][
            "check_parameters_default_constructible"
        ] = "transformer has 1 mandatory parameter"
        return tags_dict

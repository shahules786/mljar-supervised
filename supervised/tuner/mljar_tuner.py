import os
import copy
import json
import numpy as np
import pandas as pd

from supervised.tuner.random_parameters import RandomParameters
from supervised.algorithms.registry import AlgorithmsRegistry
from supervised.tuner.preprocessing_tuner import PreprocessingTuner
from supervised.tuner.hill_climbing import HillClimbing
from supervised.algorithms.registry import (
    BINARY_CLASSIFICATION,
    MULTICLASS_CLASSIFICATION,
    REGRESSION,
)

import logging
from supervised.utils.config import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class MljarTuner:
    def __init__(
        self,
        tuner_params,
        algorithms,
        ml_task,
        validation_strategy,
        explain_level,
        data_info,
        golden_features,
        features_selection,
        train_ensemble,
        stack_models,
        seed,
    ):
        logger.debug("MljarTuner.__init__")
        self._start_random_models = tuner_params.get("start_random_models", 5)
        self._hill_climbing_steps = tuner_params.get("hill_climbing_steps", 3)
        self._top_models_to_improve = tuner_params.get("top_models_to_improve", 3)
        self._algorithms = algorithms
        self._ml_task = ml_task
        self._validation_strategy = validation_strategy
        self._explain_level = explain_level
        self._data_info = data_info
        self._golden_features = golden_features
        self._features_selection = features_selection
        self._train_ensemble = train_ensemble
        self._stack_models = stack_models
        self._seed = seed

        self._unique_params_keys = []

    def steps(self):

        all_steps = [
            "simple_algorithms",
            "default_algorithms",
            # "not_so_random",
            # "golden_features",
            # "features_selection",
            # "hill_climbing",
            # "ensemble",
            # "stack",
            # "ensemble_stack",
        ]
        if self._start_random_models > 1:
            all_steps += ["not_so_random"]
        if self._golden_features:
            all_steps += ["golden_features"]
        if self._features_selection:
            all_steps += ["insert_random_feature"]
            all_steps += ["features_selection"]
        for i in range(self._hill_climbing_steps):
            all_steps += [f"hill_climbing_{i+1}"]
        if self._train_ensemble:
            all_steps += ["ensemble"]
        if self._stack_models:
            all_steps += ["stack"]
            if self._train_ensemble:
                all_steps += ["ensemble_stacked"]
        return all_steps

    def get_model_name(self, model_type, models_cnt, special=""):
        return f"{models_cnt}_" + special + model_type.replace(" ", "")

    def generate_params(self, step, models, results_path, stacked_models):

        models_cnt = len(models)
        if step == "simple_algorithms":
            return self.simple_algorithms_params()
        elif step == "default_algorithms":
            return self.default_params(models_cnt)
        elif step == "not_so_random":
            return self.get_not_so_random_params(models_cnt)
        elif step == "golden_features":
            return self.get_golden_features_params(models, results_path)
        elif step == "insert_random_feature":
            return self.get_params_to_insert_random_feature(models)
        elif step == "features_selection":
            return self.get_features_selection_params(models, results_path)
        elif "hill_climbing" in step:
            return self.get_hill_climbing_params(models)
        elif step == "ensemble":
            return [
                {
                    "model_type": "ensemble",
                    "is_stacked": False,
                    "name": "Ensemble",
                    "status": "initialized",
                    "final_loss": None,
                    "train_time": None,
                }
            ]
        elif step == "stack":
            return self.get_params_stack_models(stacked_models)
        elif step == "ensemble_stacked":

            # do we have stacked models?
            any_stacked = False
            for m in models:
                if m._is_stacked:
                    any_stacked = True
            if not any_stacked:
                return []

            return [
                {
                    "model_type": "ensemble",
                    "is_stacked": True,
                    "name": "Ensemble_Stacked",
                    "status": "initialized",
                    "final_loss": None,
                    "train_time": None,
                }
            ]

        # didnt find anything matching the step, return empty array
        return []

    def get_params_stack_models(self, stacked_models):
        if stacked_models is None or len(stacked_models) == 0:
            return []

        X_train_stacked_path = ""
        added_columns = []

        generated_params = []
        # resue old params
        for m in stacked_models:
            # print(m.get_type())
            # use only Xgboost, LightGBM and CatBoost as stacked models
            if m.get_type() not in ["Xgboost", "LightGBM", "CatBoost"]:
                continue

            params = copy.deepcopy(m.params)

            params["validation_strategy"]["X_path"] = params["validation_strategy"][
                "X_path"
            ].replace("X.parquet", "X_stacked.parquet")

            params["name"] = params["name"] + "_Stacked"
            params["is_stacked"] = True
            # print(params)
            params["status"] = "initialized"
            params["final_loss"] = None
            params["train_time"] = None

            if "model_architecture_json" in params["learner"]:
                # the new model will be created with wider input size
                del params["learner"]["model_architecture_json"]

            if self._ml_task == REGRESSION:
                # scale added predictions in regression if the target was scaled (in the case of NN)
                # this piece of code might not work, leave it as it is, because NN is not used for training with Stacked Data
                target_preprocessing = params["preprocessing"]["target_preprocessing"]
                scale = None
                if "scale_log_and_normal" in target_preprocessing:
                    scale = "scale_log_and_normal"
                elif "scale_normal" in target_preprocessing:
                    scale = "scale_normal"
                if scale is not None:
                    for col in added_columns:
                        params["preprocessing"]["columns_preprocessing"][col] = [scale]

            generated_params += [params]
        return generated_params

    def simple_algorithms_params(self):
        models_cnt = 0
        generated_params = []
        for model_type in ["Baseline", "Decision Tree", "Linear"]:
            if model_type not in self._algorithms:
                continue
            models_to_check = 1
            if model_type == "Decision Tree":
                models_to_check = min(3, self._start_random_models)
            for i in range(models_to_check):
                logger.info(f"Generate parameters for {model_type} (#{models_cnt + 1})")
                params = self._get_model_params(model_type, seed=i + 1)
                if params is None:
                    continue

                params["name"] = self.get_model_name(model_type, models_cnt + 1)
                params["status"] = "initialized"
                params["final_loss"] = None
                params["train_time"] = None

                unique_params_key = MljarTuner.get_params_key(params)
                if unique_params_key not in self._unique_params_keys:
                    generated_params += [params]
                    self._unique_params_keys += [unique_params_key]
                    models_cnt += 1
        return generated_params

    def skip_if_rows_cols_limit(self, model_type):

        max_rows_limit = AlgorithmsRegistry.get_max_rows_limit(
            self._ml_task, model_type
        )
        max_cols_limit = AlgorithmsRegistry.get_max_cols_limit(
            self._ml_task, model_type
        )

        if max_rows_limit is not None:
            if self._data_info["rows"] > max_rows_limit:
                return True
        if max_cols_limit is not None:
            if self._data_info["cols"] > max_cols_limit:
                return True

        return False

    def default_params(self, models_cnt):

        generated_params = []
        for model_type in [
            "Random Forest",
            "Extra Trees",
            "Xgboost",
            "LightGBM",
            "CatBoost",
            "Neural Network",
            "Nearest Neighbors",
            "MLP",
        ]:
            if model_type not in self._algorithms:
                continue

            if self.skip_if_rows_cols_limit(model_type):
                continue

            logger.info(f"Get default parameters for {model_type} (#{models_cnt + 1})")
            params = self._get_model_params(
                model_type, seed=models_cnt + 1, params_type="default"
            )
            if params is None:
                continue
            params["name"] = self.get_model_name(
                model_type, models_cnt + 1, special="Default_"
            )
            params["status"] = "initialized"
            params["final_loss"] = None
            params["train_time"] = None

            unique_params_key = MljarTuner.get_params_key(params)
            if unique_params_key not in self._unique_params_keys:
                generated_params += [params]
                self._unique_params_keys += [unique_params_key]
                models_cnt += 1
        return generated_params

    def get_not_so_random_params(self, models_cnt):

        generated_params = []

        for model_type in [
            "Xgboost",
            "LightGBM",
            "CatBoost",
            "Random Forest",
            "Extra Trees",
            "Neural Network",
            "Nearest Neighbors",
            "MLP",
        ]:
            if model_type not in self._algorithms:
                continue

            if self.skip_if_rows_cols_limit(model_type):
                continue
            # minus 1 because already have 1 default
            for i in range(self._start_random_models - 1):

                logger.info(
                    f"Generate not-so-random parameters for {model_type} (#{models_cnt+1})"
                )
                params = self._get_model_params(model_type, seed=i + 1)
                if params is None:
                    continue

                params["name"] = self.get_model_name(model_type, models_cnt + 1)
                params["status"] = "initialized"
                params["final_loss"] = None
                params["train_time"] = None

                unique_params_key = MljarTuner.get_params_key(params)
                if unique_params_key not in self._unique_params_keys:
                    generated_params += [params]
                    self._unique_params_keys += [unique_params_key]
                    models_cnt += 1

        # shuffle params - switch off
        # np.random.shuffle(generated_params)
        return generated_params

    def get_hill_climbing_params(self, current_models):

        # second, hill climbing
        # for _ in range(self._hill_climbing_steps):
        # just do one step
        # get models orderer by loss
        # TODO: refactor this callbacks.callbacks[0]
        scores = [m.get_final_loss() for m in current_models]
        model_types = [m.get_type() for m in current_models]
        df_models = pd.DataFrame(
            {"model": current_models, "score": scores, "model_type": model_types}
        )
        # do group by for debug reason
        df_models = df_models.groupby("model_type").apply(
            lambda x: x.sort_values("score")
        )
        unique_model_types = np.unique(df_models.model_type)

        generated_params = []
        for m_type in unique_model_types:
            if m_type in ["Baseline", "Decision Tree", "Linear", "Nearest Neighbors"]:
                # dont tune Baseline and Decision Tree
                continue
            models = df_models[df_models.model_type == m_type]["model"]

            for i in range(min(self._top_models_to_improve, len(models))):
                m = models[i]

                for p in HillClimbing.get(
                    m.params.get("learner"),
                    self._ml_task,
                    len(current_models) + self._seed,
                ):

                    model_indices = [
                        int(m.get_name().split("_")[0]) for m in current_models
                    ]
                    model_max_index = np.max(model_indices)

                    logger.info(
                        "Hill climbing step, for model #{0}".format(model_max_index + 1)
                    )
                    if p is not None:
                        all_params = copy.deepcopy(m.params)
                        all_params["learner"] = p

                        all_params["name"] = self.get_model_name(
                            all_params["learner"]["model_type"],
                            model_max_index + 1 + len(generated_params),
                        )

                        if "golden_features" in all_params["preprocessing"]:
                            all_params["name"] += "_GoldenFeatures"
                        if "drop_features" in all_params["preprocessing"] and len(
                            all_params["preprocessing"]["drop_features"]
                        ):
                            all_params["name"] += "_SelectedFeatures"
                        all_params["status"] = "initialized"
                        all_params["final_loss"] = None
                        all_params["train_time"] = None
                        unique_params_key = MljarTuner.get_params_key(all_params)
                        if unique_params_key not in self._unique_params_keys:
                            self._unique_params_keys += [unique_params_key]
                            generated_params += [all_params]
        return generated_params

    def get_golden_features_params(self, current_models, results_path):
        # get models orderer by loss
        # TODO: refactor this callbacks.callbacks[0]
        scores = [m.get_final_loss() for m in current_models]
        model_types = [m.get_type() for m in current_models]
        df_models = pd.DataFrame(
            {"model": current_models, "score": scores, "model_type": model_types}
        )
        # do group by for debug reason
        df_models = df_models.groupby("model_type").apply(
            lambda x: x.sort_values("score")
        )
        unique_model_types = np.unique(df_models.model_type)

        generated_params = []
        for m_type in unique_model_types:
            # try to add golden features only for below algorithms
            if m_type not in ["Xgboost", "LightGBM", "CatBoost"]:
                continue
            models = df_models[df_models.model_type == m_type]["model"]

            for i in range(min(1, len(models))):
                m = models[i]

                params = copy.deepcopy(m.params)
                params["preprocessing"]["golden_features"] = {
                    "results_path": results_path,
                    "ml_task": self._ml_task,
                }
                params["name"] += "_GoldenFeatures"
                params["status"] = "initialized"
                params["final_loss"] = None
                params["train_time"] = None

                if "model_architecture_json" in params["learner"]:
                    del params["learner"]["model_architecture_json"]
                unique_params_key = MljarTuner.get_params_key(params)
                if unique_params_key not in self._unique_params_keys:
                    self._unique_params_keys += [unique_params_key]
                    generated_params += [params]
        return generated_params

    def get_params_to_insert_random_feature(self, current_models):
        # get models orderer by loss
        # TODO: refactor this callbacks.callbacks[0]
        scores = [m.get_final_loss() for m in current_models]
        model_types = [m.get_type() for m in current_models]
        df_models = pd.DataFrame(
            {"model": current_models, "score": scores, "model_type": model_types}
        )
        df_models.sort_values(by="score", ascending=True, inplace=True)

        m = df_models.iloc[0]["model"]

        params = copy.deepcopy(m.params)
        params["preprocessing"]["add_random_feature"] = True
        params["name"] += "_RandomFeature"
        params["status"] = "initialized"
        params["final_loss"] = None
        params["train_time"] = None
        params["explain_level"] = 1
        if "model_architecture_json" in params["learner"]:
            del params["learner"]["model_architecture_json"]

        unique_params_key = MljarTuner.get_params_key(params)
        if unique_params_key not in self._unique_params_keys:
            self._unique_params_keys += [unique_params_key]
            return [params]
        return None

    def get_features_selection_params(self, current_models, results_path):

        fname = os.path.join(results_path, "drop_features.json")
        if not os.path.exists(fname):
            # print("The file with features to drop is missing")
            return None

        drop_features = json.load(open(fname, "r"))
        print("Drop features", drop_features)

        # in case of droping only one feature (random_feature)
        # skip this step
        if len(drop_features) <= 1:
            return None
        # get models orderer by loss
        # TODO: refactor this callbacks.callbacks[0]
        scores = [m.get_final_loss() for m in current_models]
        model_types = [m.get_type() for m in current_models]
        df_models = pd.DataFrame(
            {"model": current_models, "score": scores, "model_type": model_types}
        )
        # do group by for debug reason
        df_models = df_models.groupby("model_type").apply(
            lambda x: x.sort_values("score")
        )
        unique_model_types = np.unique(df_models.model_type)

        generated_params = []
        for m_type in unique_model_types:
            # try to add golden features only for below algorithms
            if m_type not in [
                "Xgboost",
                "LightGBM",
                "CatBoost",
                "Neural Network",
                "Random Forest",
                "Extra Trees",
            ]:
                continue
            models = df_models[df_models.model_type == m_type]["model"]

            for i in range(min(1, len(models))):
                m = models[i]

                params = copy.deepcopy(m.params)
                params["preprocessing"]["drop_features"] = drop_features
                params["name"] += "_SelectedFeatures"
                params["status"] = "initialized"
                params["final_loss"] = None
                params["train_time"] = None
                if "model_architecture_json" in params["learner"]:
                    del params["learner"]["model_architecture_json"]
                unique_params_key = MljarTuner.get_params_key(params)
                if unique_params_key not in self._unique_params_keys:
                    self._unique_params_keys += [unique_params_key]
                    generated_params += [params]
        return generated_params

    def _get_model_params(self, model_type, seed, params_type="random"):
        model_info = AlgorithmsRegistry.registry[self._ml_task][model_type]

        model_params = None
        if params_type == "default":

            model_params = model_info["default_params"]
            model_params["seed"] = seed

        else:
            model_params = RandomParameters.get(model_info["params"], seed + self._seed)
        if model_params is None:
            return None

        required_preprocessing = model_info["required_preprocessing"]
        model_additional = model_info["additional"]
        preprocessing_params = PreprocessingTuner.get(
            required_preprocessing, self._data_info, self._ml_task
        )

        model_params = {
            "additional": model_additional,
            "preprocessing": preprocessing_params,
            "validation_strategy": self._validation_strategy,
            "learner": {
                "model_type": model_info["class"].algorithm_short_name,
                "ml_task": self._ml_task,
                **model_params,
            },
        }

        if self._data_info.get("num_class") is not None:
            model_params["learner"]["num_class"] = self._data_info.get("num_class")

        model_params["ml_task"] = self._ml_task
        model_params["explain_level"] = self._explain_level

        return model_params

    @staticmethod
    def get_params_key(params):
        key = "key_"
        for main_key in ["preprocessing", "learner"]:
            key += main_key
            for k, v in params[main_key].items():
                if k == "seed":
                    continue
                key += "_{}_{}".format(k, v)
        return key

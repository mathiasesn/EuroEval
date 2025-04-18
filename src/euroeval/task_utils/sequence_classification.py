"""Utility functions related to the sequence-classification task group."""

import logging
import re
import typing as t

import evaluate
import Levenshtein
import numpy as np
from evaluate import EvaluationModule

from ..data_models import BenchmarkConfig, GenerativeModelOutput
from ..exceptions import InvalidBenchmark
from ..utils import log_once, raise_if_model_output_contains_nan_values

if t.TYPE_CHECKING:
    from transformers import EvalPrediction

    from ..data_models import DatasetConfig
    from ..types import Labels, Predictions


logger = logging.getLogger("euroeval")


def compute_metrics(
    model_outputs_and_labels: "tuple[Predictions, Labels] | EvalPrediction",
    dataset_config: "DatasetConfig",
    benchmark_config: "BenchmarkConfig",
) -> dict[str, float]:
    """Compute the metrics needed for evaluation.

    Args:
        model_outputs_and_labels:
            The first sequence contains the model outputs and the second sequence
            contains the true labels.
        dataset_config:
            The configuration of the dataset.
        benchmark_config:
            The configuration of the benchmark.

    Returns:
        A dictionary with the names of the metrics as keys and the metric values as
        values.
    """
    model_outputs, labels = model_outputs_and_labels
    label2id = {label: idx for idx, label in dataset_config.id2label.items()}

    # If the model outputs is a pair, then the first element corresponds to the model
    # predictions
    if isinstance(model_outputs, tuple) and len(model_outputs) == 2:
        model_outputs = model_outputs[0]

    metrics = {
        metric_cfg.name: (
            evaluate.load(
                path=metric_cfg.huggingface_id, cache_dir=benchmark_config.cache_dir
            )
            if metric_cfg.huggingface_id != ""
            else None
        )
        for metric_cfg in dataset_config.task.metrics
    }

    model_output_dtype = np.asarray(model_outputs).dtype
    if model_output_dtype in [np.float16, np.float32, np.float64]:
        predictions = np.asarray(model_outputs).argmax(axis=-1)
    else:
        predictions = model_outputs

    assert not isinstance(model_outputs, tuple)
    raise_if_model_output_contains_nan_values(model_output=model_outputs)

    prompt_label_to_label_mapping = {
        prompt_label: label
        for label, prompt_label in dataset_config.prompt_label_mapping.items()
    }
    predictions = [
        (
            label2id[prompt_label_to_label_mapping[pred.lower()]]
            if isinstance(pred, str)
            else pred
        )
        for pred in predictions
    ]

    label_ids = [
        label2id[label.lower()] if isinstance(label, str) else label for label in labels
    ]

    results: dict[str, float] = dict()
    for cfg in dataset_config.task.metrics:
        metric = metrics[cfg.name]
        assert isinstance(metric, EvaluationModule)
        score_dict: dict[str, float] | None = metric.compute(
            predictions=predictions, references=label_ids, **cfg.compute_kwargs
        )

        # The metric returns None if we are running on multi-GPU and the current
        # process is not the main process
        if score_dict is not None:
            scores = score_dict[cfg.results_key]
            if isinstance(scores, list):
                scores = sum(scores) / len(scores)
            results[cfg.name] = scores

    return results


def extract_labels_from_generation(
    input_batch: dict[str, list],
    model_output: GenerativeModelOutput,
    dataset_config: "DatasetConfig",
    first_label_token_mapping: dict[str, str] | bool,
) -> list[str]:
    """Extract the predicted labels from the generated output.

    Args:
        input_batch:
            The input batch, where the keys are the feature names and the values
            are lists with the feature values.
        model_output:
            The raw generated output of the model.
        dataset_config:
            The configuration of the dataset.
        first_label_token_mapping:
            A mapping from labels to the first token in each label, or alternatively a
            Boolean value indicating whether the model should output scores (if the
            mapping is outputted then the model will always output scores).

    Returns:
        The predicted labels.
    """
    if model_output.scores is not None:
        return get_closest_logprobs_labels(
            generation_logprobs=model_output.scores,
            dataset_config=dataset_config,
            first_label_token_mapping=first_label_token_mapping,
        )
    else:
        return get_closest_word_edit_labels(
            generated_sequences=model_output.sequences, dataset_config=dataset_config
        )


def get_closest_logprobs_labels(
    generation_logprobs: list[list[list[tuple[str, float]]]],
    dataset_config: "DatasetConfig",
    first_label_token_mapping: dict[str, str] | bool,
) -> list[str]:
    """Get the labels with the highest predicted logprob value.

    In case a candidate label is split into multiple tokens, we only use the first
    token to compute the logprob value. E.g., if the candidate label "positive" is
    tokenised as ["pos", "itive"], we only use the logprob value of "pos" to
    represent the logprob value of the entire label.

    Args:
        generation_logprobs:
            The logprobs of the generated tokens, for all samples in the batch. Of shape
            (batch_size, num_tokens, num_logprobs).
        dataset_config:
            The configuration of the dataset.
        first_label_token_mapping:
            A mapping from labels to the first token in each label, or alternatively a
            Boolean value indicating whether the model should output scores (if the
            mapping is outputted then the model will always output scores).

    Returns:
        The predicted labels.

    Raises:
        InvalidBenchmark:
            If no candidate label can be found for any of the generated labels.
    """
    english_labels = list(dataset_config.id2label.values())
    english2local = dataset_config.prompt_label_mapping
    candidate_labels = [english2local[lbl].lower() for lbl in english_labels]

    output_labels: list[str] = list()
    for sample in generation_logprobs:
        for logprob_list in sample:
            generated_labels = [
                re.sub(
                    pattern=r"^[^a-zæøåüöä]+|[^a-zæøåüöä]+$",
                    repl="",
                    string=label.lower(),
                )
                for label, _ in logprob_list
            ]
            generated_labels = [label for label in generated_labels if label != ""]

            # We want to use the first generated label which contains a unique candidate
            # label, as the output label
            output_label: str | None = None
            previously_generated_labels: list[str] = list()
            for label_idx, generated_label in enumerate(generated_labels):
                generated_label = "".join(previously_generated_labels) + generated_label

                # Get the candidate labels that starts with the generated label
                if isinstance(first_label_token_mapping, dict):
                    if any(
                        candidate_label not in first_label_token_mapping
                        for candidate_label in candidate_labels
                    ):
                        raise InvalidBenchmark(
                            "There is a label not present in the first label token "
                            "mapping - this should never happen! Please report this "
                            "issue to the EuroEval team at "
                            "github.com/EuroEval/EuroEval/issues."
                        )

                    candidate_output_labels = {
                        candidate_label
                        for candidate_label in candidate_labels
                        if generated_label
                        in {candidate_label, first_label_token_mapping[candidate_label]}
                    }
                else:
                    candidate_output_labels = {
                        candidate_label
                        for candidate_label in candidate_labels
                        if candidate_label.startswith(generated_label)
                    }

                # If we can uniquely determine the output label, we break the loop. If
                # there are multiple possible labels then we store the current one, and
                # concatenate it with the next generated label. We can only do this if
                # the current one is the first one, however, since we're using greedy
                # sampling. In case this happens for a label that is not the first one,
                # we warn the user.
                if len(candidate_output_labels) == 1:
                    output_label = candidate_output_labels.pop()
                    break
                elif len(candidate_output_labels) > 1:
                    if label_idx == 0:
                        previously_generated_labels.append(generated_label)
                    else:
                        output_label = candidate_output_labels.pop()
                        candidate_output_labels.add(output_label)
                        raise InvalidBenchmark(
                            "Multiple candidate labels found for the generated label "
                            f"{generated_label!r}: {candidate_output_labels}. Since "
                            "this is not the first generated label, we cannot "
                            "concatenate it with the next generated label. We are thus "
                            f"forced to use the arbitrary {output_label!r} as the "
                            "output label, potentially resulting in worse performance. "
                            "Please report this issue to the EuroEval team at "
                            "github.com/EuroEval/EuroEval/issues."
                        )
                elif len(candidate_output_labels) == 0:
                    logger.debug(
                        f"No candidate label found for the generated label "
                        f"{generated_label!r}. The generated label is thus ignored."
                    )

            if output_label is not None:
                output_labels.append(output_label)
                break
        else:
            if len(sample) == 0:
                log_once(
                    "The model outputted an empty string, so no candidate labels could "
                    f"be determined. Using {candidate_labels[0]!r} as the output "
                    "label.",
                    level=logging.DEBUG,
                )
            else:
                log_once(
                    "Could not find a candidate label for any of the generated "
                    f"labels in the sample {sample}. Using {candidate_labels[0]!r} "
                    "as the output label.",
                    level=logging.DEBUG,
                )
            output_labels.append(candidate_labels[0])

    assert len(output_labels) == len(generation_logprobs)
    return output_labels


def get_closest_word_edit_labels(
    generated_sequences: list[str], dataset_config: "DatasetConfig"
) -> list[str]:
    """Get the labels with the smallest edit distance to the predicted labels.

    Args:
        generated_sequences:
            The generated sequences from the model.
        dataset_config:
            The configuration of the dataset.

    Returns:
        The candidate labels with the smallest edit distance to the predicted labels.
    """
    candidate_labels = [
        dataset_config.prompt_label_mapping[lbl]
        for lbl in dataset_config.id2label.values()
    ]
    new_predicted_labels: list[str] = list()
    for predicted_label in generated_sequences:
        edit_distances = [
            Levenshtein.distance(s1=predicted_label.lower(), s2=candidate_label.lower())
            for candidate_label in candidate_labels
        ]
        closest_label = candidate_labels[np.argmin(edit_distances).item()]
        new_predicted_labels.append(closest_label)
    return new_predicted_labels

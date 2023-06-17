"""Sequence classification benchmark dataset."""

import logging
from functools import partial

from datasets.arrow_dataset import Dataset
from transformers import BatchEncoding
from transformers.data.data_collator import DataCollator, DataCollatorWithPadding

from .benchmark_dataset import BenchmarkDataset
from .exceptions import InvalidBenchmark
from .model_setups import Tokenizer
from .utils import get_special_token_metadata

logger = logging.getLogger(__name__)


class SequenceClassification(BenchmarkDataset):
    """Sequence classification benchmark dataset.

    Args:
        dataset_config (DatasetConfig):
            The dataset configuration.
        benchmark_config (BenchmarkConfig):
            The benchmark configuration.

    Attributes:
        dataset_config (DatasetConfig):
            The configuration of the dataset.
        benchmark_config (BenchmarkConfig):
            The configuration of the benchmark.
    """

    def _preprocess_data(self, dataset: Dataset, **kwargs) -> Dataset:
        """Preprocess a dataset by tokenizing and aligning the labels.

        Args:
            dataset (Hugging Face dataset):
                The dataset to preprocess.
            kwargs:
                Extra keyword arguments containing objects used in preprocessing the
                dataset.

        Returns:
            Hugging Face dataset:
                The preprocessed dataset.
        """
        tokenizer: Tokenizer = kwargs["tokenizer"]

        # Extract special token metadata from the tokenizer
        special_token_metadata = get_special_token_metadata(tokenizer=tokenizer)
        has_cls_token = special_token_metadata["has_cls_token"]
        has_sep_token = special_token_metadata["has_sep_token"]
        cls_token = special_token_metadata["cls_token"]
        sep_token = special_token_metadata["sep_token"]

        if kwargs["model_config"].task == "text-generation":
            dataset = dataset.map(self._apply_few_shot_prompt, batched=True)
            breakpoint()

        def tokenise(examples: dict) -> BatchEncoding:
            # If the tokenizer is not adding special tokens, then we add them manually.
            # We don't need this when performing few-shot evaluations, so in that case
            # we don't add the special tokens.
            if (
                not has_cls_token
                and not has_sep_token
                and cls_token is not None
                and sep_token is not None
                and kwargs["model_config"].task != "text-generation"
            ):
                examples["text"] = [
                    f"{cls_token}{doc}{sep_token}" for doc in examples["text"]
                ]

            return tokenizer(
                text=examples["text"],
                truncation=True,
                padding=False,
            )

        tokenised = dataset.map(tokenise, batched=True, load_from_cache_file=False)

        if kwargs["model_config"].task != "text-generation":
            numericalise = partial(
                self._create_numerical_labels,
                label2id=kwargs["hf_model_config"].label2id,
            )
            return tokenised.map(
                numericalise, batched=True, load_from_cache_file=False
            ).remove_columns(["text"])
        else:
            return tokenised

    def _apply_few_shot_prompt(self, examples: dict) -> dict:
        examples["text"] = [
            self.dataset_config.prompt_template.format(text=text).strip()
            for text in examples["text"]
        ]
        return examples

    def _create_numerical_labels(self, examples: dict, label2id: dict) -> dict:
        try:
            examples["label"] = [label2id[lbl.upper()] for lbl in examples["label"]]
        except KeyError:
            raise InvalidBenchmark(
                f"One of the labels in the dataset, {examples['label'].upper()}, does "
                f"not occur in the label2id dictionary {label2id}."
            )
        return examples

    def _load_data_collator(self, tokenizer: Tokenizer | None = None) -> DataCollator:
        """Load the data collator used to prepare samples during finetuning.

        Args:
            tokenizer (Tokenizer or None, optional):
                A tokenizer. Can be None if the tokenizer is not used in the
                initialisation of the data collator. Defaults to None.

        Returns:
            Hugging Face data collator:
                The data collator.
        """
        return DataCollatorWithPadding(tokenizer, padding="longest")

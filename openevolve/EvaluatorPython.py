"""
Evaluation system for OpenEvolve
"""

import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from typing import Dict, Optional
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import traceback

from openevolve.EvaluatorInterface import Evaluator
from openevolve.config import EvaluatorConfig
from openevolve.llm.ensemble import LLMEnsemble
from openevolve.utils.async_utils import TaskPool, run_in_executor
from openevolve.prompt.sampler import PromptSampler
from openevolve.utils.format_utils import format_metrics_safe

logger = logging.getLogger(__name__)


class EvaluatorPython(Evaluator):
    """
    Evaluates programs and assigns scores

    The evaluator is responsible for executing programs, measuring their performance,
    and assigning scores based on the evaluation criteria.
    """

    def __init__(
        self,
        config: EvaluatorConfig,
        evaluation_file: str,
        llm_ensemble: Optional[LLMEnsemble] = None,
        prompt_sampler: Optional[PromptSampler] = None,
    ):
        super().__init__(config)
        self.config = config
        self.evaluation_file = evaluation_file
        self.llm_ensemble = llm_ensemble
        self.prompt_sampler = prompt_sampler

        # Create a task pool for parallel evaluation
        self.task_pool = TaskPool(max_concurrency=config.parallel_evaluations)

        # Set up evaluation function if file exists
        self._load_evaluation_function()

        logger.info(f"Initialized evaluator with {evaluation_file}")

    def _load_evaluation_function(self) -> None:
        """Load the evaluation function from the evaluation file"""
        if not os.path.exists(self.evaluation_file):
            raise ValueError(f"Evaluation file {self.evaluation_file} not found")

        try:
            spec = importlib.util.spec_from_file_location("evaluation_module", self.evaluation_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"Failed to load spec from {self.evaluation_file}")

            module = importlib.util.module_from_spec(spec)
            sys.modules["evaluation_module"] = module
            spec.loader.exec_module(module)

            if not hasattr(module, "evaluate"):
                raise AttributeError(
                    f"Evaluation file {self.evaluation_file} does not contain an 'evaluate' function"
                )

            self.evaluate_function = module.evaluate
            logger.info(f"Successfully loaded evaluation function from {self.evaluation_file}")
        except Exception as e:
            logger.error(f"Error loading evaluation function: {str(e)}")
            raise

    async def evaluate_program(
        self,
        program_code: str,
        program_id: str = "",
    ) -> Dict[str, float]:

        start_time = time.time()
        program_id_str = f" {program_id}" if program_id else ""

        # Retry logic for evaluation
        last_exception = None
        for attempt in range(self.config.max_retries + 1):
            # Create a temporary file for the program
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as temp_file:
                temp_file.write(program_code.encode("utf-8"))
                temp_file_path = temp_file.name

            try:
                # Run evaluation
                if self.config.cascade_evaluation:
                    # Run cascade evaluation
                    metrics = await self._cascade_evaluate(temp_file_path)
                else:
                    # Run direct evaluation
                    metrics = await self._direct_evaluate(temp_file_path)

                # Add LLM feedback if configured
                if self.config.use_llm_feedback and self.llm_ensemble:
                    feedback_metrics = await self._llm_evaluate(program_code)

                    # Combine metrics
                    for name, value in feedback_metrics.items():
                        metrics[f"llm_{name}"] = value * self.config.llm_feedback_weight

                elapsed = time.time() - start_time
                logger.info(
                    f"Evaluated program{program_id_str} in {elapsed:.2f}s: "
                    f"{format_metrics_safe(metrics)}"
                )

                return metrics

            except Exception as e:
                last_exception = e
                logger.warning(
                    f"Evaluation attempt {attempt + 1}/{self.config.max_retries + 1} failed for program{program_id_str}: {str(e)}"
                )

                # If this is not the last attempt, wait a bit before retrying
                if attempt < self.config.max_retries:
                    await asyncio.sleep(1.0)  # Wait 1 second before retry

            finally:
                # Clean up temporary file
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)

        # All retries failed
        logger.error(
            f"All evaluation attempts failed for program{program_id_str}. Last error: {str(last_exception)}"
        )
        return {"error": 0.0}

    @run_in_executor
    def _direct_evaluate(self, program_path: str) -> Dict[str, float]:
        """
        Directly evaluate a program using the evaluation function

        Args:
            program_path: Path to the program file

        Returns:
            Dictionary of metric name to score
        """
        try:
            # Run the evaluation with timeout
            result = self.evaluate_function(program_path)

            # Validate result
            if not isinstance(result, dict):
                logger.warning(f"Evaluation returned non-dictionary result: {result}")
                return {"error": 0.0}

            return result

        except Exception as e:
            logger.error(f"Error in direct evaluation: {str(e)}")
            return {"error": 0.0}

    async def _cascade_evaluate(self, program_path: str) -> Dict[str, float]:
        """
        Run cascade evaluation with increasingly challenging test cases

        Args:
            program_path: Path to the program file

        Returns:
            Dictionary of metric name to score
        """
        # Import the evaluation module to get cascade functions if they exist
        try:
            spec = importlib.util.spec_from_file_location("evaluation_module", self.evaluation_file)
            if spec is None or spec.loader is None:
                return await self._direct_evaluate(program_path)

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Check if cascade functions exist
            if not hasattr(module, "evaluate_stage1"):
                return await self._direct_evaluate(program_path)

            # Run first stage
            try:
                stage1_result = await run_in_executor(module.evaluate_stage1)(program_path)
                if not isinstance(stage1_result, dict):
                    logger.warning(
                        f"Stage 1 evaluation returned non-dictionary result: {stage1_result}"
                    )
                    return {"error": 0.0}
            except Exception as e:
                logger.error(f"Error in stage 1 evaluation: {str(e)}")
                return {"error": 0.0}

            # Check threshold
            if not self._passes_threshold(stage1_result, self.config.cascade_thresholds[0]):
                return stage1_result

            # Check if second stage exists
            if not hasattr(module, "evaluate_stage2"):
                return stage1_result

            # Run second stage
            try:
                stage2_result = await run_in_executor(module.evaluate_stage2)(program_path)
                if not isinstance(stage2_result, dict):
                    logger.warning(
                        f"Stage 2 evaluation returned non-dictionary result: {stage2_result}"
                    )
                    return stage1_result
            except Exception as e:
                logger.error(f"Error in stage 2 evaluation: {str(e)}")
                return stage1_result

            # Merge results
            result = {}
            # Convert all values to float to avoid type errors
            for name, value in stage1_result.items():
                if isinstance(value, (int, float)) and name != "error":
                    result[name] = float(value)

            for name, value in stage2_result.items():
                if isinstance(value, (int, float)) and name != "error":
                    result[name] = float(value)

            # Check threshold
            if len(self.config.cascade_thresholds) < 2 or not self._passes_threshold(
                result, self.config.cascade_thresholds[1]
            ):
                return result

            # Check if third stage exists
            if not hasattr(module, "evaluate_stage3"):
                return result

            # Run third stage
            try:
                stage3_result = await run_in_executor(module.evaluate_stage3)(program_path)
                if not isinstance(stage3_result, dict):
                    logger.warning(
                        f"Stage 3 evaluation returned non-dictionary result: {stage3_result}"
                    )
                    return result
            except Exception as e:
                logger.error(f"Error in stage 3 evaluation: {str(e)}")
                return result

            # Merge results
            for name, value in stage3_result.items():
                if isinstance(value, (int, float)) and name != "error":
                    result[name] = float(value)

            return result

        except Exception as e:
            logger.error(f"Error in cascade evaluation: {str(e)}")
            return {"error": 0.0}

    async def _llm_evaluate(self, program_code: str) -> Dict[str, float]:
        """
        Use LLM to evaluate code quality

        Args:
            program_code: Code to evaluate

        Returns:
            Dictionary of metric name to score
        """
        if not self.llm_ensemble:
            return {}

        try:
            # Create prompt for LLM
            prompt = self.prompt_sampler.build_prompt(
                current_program=program_code, template_key="evaluation"
            )

            # Get LLM response
            responses = await self.llm_ensemble.generate_all_with_context(
                prompt["system"], [{"role": "user", "content": prompt["user"]}]
            )

            # Extract JSON from response
            try:
                # Try to find JSON block
                json_pattern = r"```json\n(.*?)\n```"
                import re

                avg_metrics = {}
                for i, response in enumerate(responses):
                    json_match = re.search(json_pattern, response, re.DOTALL)

                    if json_match:
                        json_str = json_match.group(1)
                    else:
                        # Try to extract JSON directly
                        json_str = response
                        # Remove non-JSON parts
                        start_idx = json_str.find("{")
                        end_idx = json_str.rfind("}") + 1
                        if start_idx >= 0 and end_idx > start_idx:
                            json_str = json_str[start_idx:end_idx]

                    # Parse JSON
                    result = json.loads(json_str)

                    # Filter all non-numeric values
                    metrics = {
                        name: float(value)
                        for name, value in result.items()
                        if isinstance(value, (int, float))
                    }

                    # Weight of the model in the ensemble
                    weight = self.llm_ensemble.weights[i] if self.llm_ensemble.weights else 1.0

                    # Average the metrics
                    for name, value in metrics.items():
                        if name in avg_metrics:
                            avg_metrics[name] += value * weight
                        else:
                            avg_metrics[name] = value * weight

                return avg_metrics

            except Exception as e:
                logger.warning(f"Error parsing LLM response: {str(e)}")
                return {}

        except Exception as e:
            logger.error(f"Error in LLM evaluation: {str(e)}")
            traceback.print_exc()
            return {}

    def _passes_threshold(self, metrics: Dict[str, float], threshold: float) -> bool:
        """
        Check if metrics pass a threshold

        Args:
            metrics: Dictionary of metric name to score
            threshold: Threshold to pass

        Returns:
            True if metrics pass threshold
        """
        if not metrics:
            return False

        # Calculate average score, skipping non-numeric values and 'error' key
        valid_metrics = []
        for name, value in metrics.items():
            # Skip 'error' keys and ensure values are numeric
            if name != "error" and isinstance(value, (int, float)):
                try:
                    valid_metrics.append(float(value))
                except (TypeError, ValueError):
                    logger.warning(f"Skipping non-numeric metric: {name}={value}")
                    continue

        if not valid_metrics:
            return False

        avg_score = sum(valid_metrics) / len(valid_metrics)
        return avg_score >= threshold

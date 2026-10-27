"""
================================================================================
SYSTEM ONE CLIENT — Ollama decision-model API
================================================================================
Thin client for Ollama serving decision models (like d3:2b or d3:4b), using the
official `ollama` Python package (>= 0.6.3, which provides Client.systemone).

A decision model does not write text. You send one `state` (here: the email)
and several typed `questions`; it answers each with a probability per option
in a single forward pass.
================================================================================
"""
import ollama

from config import OLLAMA_HOST, OLLAMA_MODEL, REQUEST_TIMEOUT


class DecisionError(RuntimeError):
    """Server unreachable, not serving a decision model, or malformed answer."""


_client = ollama.Client(host=OLLAMA_HOST, timeout=REQUEST_TIMEOUT)


def decide(state, questions, model=None, timeout=None):
    """
    Request answers for multiple typed questions based on a single state.
    Returns the `answers` dict keyed by question name (plain dicts).
    """
    model = model or OLLAMA_MODEL
    client = _client if timeout is None else ollama.Client(host=OLLAMA_HOST, timeout=timeout)

    try:
        response = client.systemone(model=model, state=state, questions=questions)
    except ollama.ResponseError as e:
        raise DecisionError(f"Ollama error for model '{model}': {e.error} (HTTP {e.status_code})") from e
    except AttributeError as e:
        raise DecisionError("ollama package too old (no Client.systemone). "
                            "Run: py -m pip install -U \"ollama>=0.6.3\"") from e
    except Exception as e:
        raise DecisionError(f"Ollama request failed: {e}") from e

    answers = response.model_dump().get("answers")
    if not isinstance(answers, dict):
        raise DecisionError(f"Response had no 'answers': {str(response)[:200]}")
    return answers


def check_server():
    """
    Verify Ollama is up AND serving a decision model by sending a
    one-question probe. Raises DecisionError.
    """
    probe = {"ping": {"type": "noul", "instructions": "Is this text a greeting?"}}
    decide("Hello", probe)

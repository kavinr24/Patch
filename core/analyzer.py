from groq import Groq

import config

_groq = Groq(api_key=config.GROQ_API_KEY)


def generate_patch(file_path: str, file_content: str, error_context: str) -> str:
    """Ask the Groq LLM for a patch for the given file based on error context.

    TODO: implement the LLM call and return a real patch.
    """
    return "PLACEHOLDER"

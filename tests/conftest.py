import os

# LiteLLM fetches a model price map from GitHub at import time unless told not to.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

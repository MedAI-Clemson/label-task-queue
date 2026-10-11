"""Print the JSON Schema of the config: ``python -m labelflow.engine.config.schema``.

This module is only the command. The schema itself is built in ``json_schema.py``, which
the package imports, so that running this module does not import it twice.
"""

from labelflow.engine.config.json_schema import config_json_schema_text

if __name__ == "__main__":
    print(config_json_schema_text(), end="")

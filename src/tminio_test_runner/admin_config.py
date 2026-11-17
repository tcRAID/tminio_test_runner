"""Parse mc's persisted config separately from its environment annotations."""
import shlex


def effective_value(output, subsystem, key, environment_key):
    """Read one setting while honoring mc's effective-environment annotation."""
    values = dict(argument.split("=", 1) for argument in parse_config(output, subsystem)[1:])
    for line in output.splitlines():
        if line.lstrip().startswith("#"):
            for token in shlex.split(line.lstrip()[1:]):
                name, separator, value = token.partition("=")
                if separator and name == environment_key:
                    return value
    assert key in values, f"{subsystem} config omitted {key}"
    return values[key]


def parse_config(output, subsystem, *, mutable_env=None):
    overrides = {}
    config_lines = []
    for line in output.splitlines():
        if line.lstrip().startswith("#"):
            for token in shlex.split(line.lstrip()[1:]):
                key, separator, value = token.partition("=")
                if separator and key.startswith("MINIO_"):
                    overrides[key] = value
        elif line.strip():
            config_lines.append(line)
    for name, required in (mutable_env or {}).items():
        if name in overrides:
            assert required is not None and overrides[name] == required, (
                f"{name} overrides dynamic configuration; remove it from the dev server "
                "before running this test"
            )
    assert len(config_lines) == 1, "expected one persisted subsystem config line"
    # mc only quotes values containing whitespace; an unquoted '#' inside a
    # persisted value is data and must survive restoration.
    arguments = shlex.split(config_lines[0])
    assert arguments and arguments[0] == subsystem, f"unexpected mc {subsystem} config output"
    assert all("=" in argument for argument in arguments[1:]), "unexpected extra config lines"
    return arguments

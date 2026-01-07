import os


def from_env(key: str, default: str) -> str:
    """Get value from environment variable or return default if empty or unset"""
    value = os.environ.get(key, '')
    return default if value is None or len(value) == 0 else value

def list_from_env(key: str) -> list:
    """Get list value from environment variable.
       List is defined as string of comma separated values"""
    value = from_env(key, '')
    return [value.strip() for value in value.split(',') if len(value.strip()) > 0]

def int_from_env(key: str, default: int) -> int:
    """Get int value from environment variable or return default if empty or unset
       Throw an error when value is not an integer"""
    value = from_env(key, '')
    if value is None or len(value) == 0:
        return default
    try:
        return int(value)
    except ValueError:
        raise Exception(f'Value of {key} is not an integer: {value}')

def map_from_env(key: str) -> dict:
    value = list_from_env(key)
    ret = {}
    for kv in value:
        key_value = kv.split('=', maxsplit=1)
        if len(key_value) != 2: raise Exception(f'Map {key} contains invalid key-value pair: {kv}')
        k, v = key_value
        ret[k] = v
    return ret
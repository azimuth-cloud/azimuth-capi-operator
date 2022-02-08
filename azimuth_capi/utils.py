import functools


def deepmerge(defaults, *overrides):
    """
    Returns a new dictionary obtained by deep-merging multiple sets of overrides
    into defaults, with precedence from right to left.
    """
    def deepmerge2(defaults, overrides):
        if not overrides:
            return defaults
        # This only needs to be a shallow copy, as we will recurse for nested dicts
        # which gives us a copy-on-write approach
        merged = dict(defaults)
        for key, value in overrides.items():
            if isinstance(value, dict):
                merged[key] = deepmerge(merged.get(key, {}), value)
            else:
                merged[key] = value
        return merged
    return functools.reduce(deepmerge2, overrides, defaults)

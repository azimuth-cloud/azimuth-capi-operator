from curses.ascii import isdigit
import functools
import re


# Regex that matches a single SemVer version with groups
SEMVER_VERSION_REGEX = (
    r"^"
    r"v?"
    r"(?P<major>\d+)"
    r"\."
    r"(?P<minor>\d+)"
    r"\."
    r"(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[a-zA-Z0-9.-]+))?"
    r"(?:\+(?P<build>[a-zA-Z0-9.-]+))?"
    r"$"
)
# Regex that matches a single SemVer version anywhere in a string without groups
SEMVER_VERSION_ANY_REGEX = r"v?\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?"
# Regex that matches a single constraint with groups
SEMVER_CONSTRAINT_REGEX = (
    r"^" +
    r"(?P<operator>==|!=|>=?|<=?)" +
    r"(?P<version>" + SEMVER_VERSION_ANY_REGEX + r")" +
    r"$"
)
# Regex that matches a constraint anywhere in a string without groups
SEMVER_CONSTRAINT_ANY_REGEX = r"(?:==|!=|>=?|<=?)" + SEMVER_VERSION_ANY_REGEX
# Regex that matches one or more constraints separated by commas
# This is used in CRD schemas, so can only use Perl-compliant features
SEMVER_RANGE_REGEX = (
    r"^" +
    SEMVER_CONSTRAINT_ANY_REGEX +
    r"(?:," + SEMVER_CONSTRAINT_ANY_REGEX + r")*" +
    r"$"
)


@functools.total_ordering
class Component:
    """
    Represents a component in a version.
    """
    def __init__(self, value):
        self.value = str(value)

    def __eq__(self, other):
        return self.value == str(other)

    def __gt__(self, other):
        other = str(other)
        # If both parts are all digits, compare them as numbers
        # If not, do a string comparison
        if self.value.isdigit() and other.isdigit():
            return int(self.value) > int(other)
        else:
            return self.value > other

    def __str__(self):
        return self.value


@functools.total_ordering
class Version:
    """
    Represents a SemVer version.
    """
    def __init__(self, version):
        match = re.match(SEMVER_VERSION_REGEX, version)
        if match is None:
            raise TypeError(f"'{version}' is not a valid SemVer version")
        self.major = Component(match.group("major"))
        self.minor = Component(match.group("minor"))
        self.patch = Component(match.group("patch"))
        # The prerelease can consist of multiple dot-separated components
        prerelease = match.group("prerelease")
        self.prerelease = (
            tuple(Component(p) for p in prerelease.split("."))
            if prerelease
            else None
        )
        # The build isn't considered for anything, so doesn't need to be a component
        self.build = match.group("build")

    def format(self, prerelease = True, build = True):
        version = f"{self.major}.{self.minor}.{self.patch}"
        if prerelease and self.prerelease:
            version = f"{version}-{'.'.join(str(p) for p in self.prerelease)}"
        if build and self.build:
            version = f"{version}+{self.build}"
        return version

    def __str__(self):
        return self.format()

    def __eq__(self, other):
        if not isinstance(other, Version):
            other = Version(other)
        # Two versions with the same build are equal
        return (
            self.major == other.major and
            self.minor == other.minor and
            self.patch == other.patch and
            self.prerelease == other.prerelease
        )

    def __gt__(self, other):
        if not isinstance(other, Version):
            other = Version(other)
        if self.major != other.major:
            return self.major > other.major
        if self.minor != other.minor:
            return self.minor > other.minor
        if self.patch != other.patch:
            return self.patch > other.patch
        # Versions with prereleases are less than those that don't
        if not self.prerelease and other.prerelease:
            return True
        if self.prerelease and not other.prerelease:
            return False
        if self.prerelease and other.prerelease:
            # Prereleases take advantage of elementwise tuple ordering
            return self.prerelease > other.prerelease
        else:
            return False


class Constraint:
    """
    Represents a single SemVer constraint.
    """
    def __init__(self, constraint):
        match = re.match(SEMVER_CONSTRAINT_REGEX, constraint)
        if match is None:
            raise TypeError(f"'{constraint}' is not a valid SemVer constraint")
        self.operator = match.group("operator")
        self.version = Version(match.group("version"))

    def __contains__(self, version):
        if not isinstance(version, Version):
            version = Version(version)
        if self.operator == "==":
            return version == self.version
        if self.operator == "!=":
            return version != self.version
        if self.operator == "<=":
            return version <= self.version
        if self.operator == "<":
            return version < self.version
        # For lower bounds, we have to decide whether to include prereleases
        if version.prerelease and not self.version.prerelease:
            return False
        if self.operator == ">=":
            return version >= self.version
        if self.operator == ">":
            return version > self.version

    @property
    def is_upper(self):
        return self.operator.startswith("<")

    @property
    def is_lower(self):
        return self.operator.startswith(">")

    def __str__(self):
        return f"{self.operator}{self.version}"


class Range:
    """
    Represents a SemVer range consisting of the union of multiple constraints.
    """
    def __init__(self, range):
        match = re.match(SEMVER_RANGE_REGEX, range)
        if match is None:
            raise TypeError(f"'{range}' is not a valid SemVer range")
        constraints = [Constraint(constraint) for constraint in range.split(",")]
        # If there is no lower bound, include >=0.0.0 implicitly so that only
        # stable releases are considered
        if not any(c.is_lower for c in constraints):
            constraints.insert(0, Constraint(">=0.0.0"))
        self.constraints = constraints

    def __contains__(self, version):
        if not isinstance(version, Version):
            version = Version(version)
        # The version must be in all the constraints
        for constraint in self.constraints:
            if version not in constraint:
                return False
        return True

    def __str__(self):
        return ",".join(str(constraint) for constraint in self.constraints)

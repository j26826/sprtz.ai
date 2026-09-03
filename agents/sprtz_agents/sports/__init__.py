"""Sport-specific moment taxonomies and analysis prompts.

Each sport contributes a :class:`SportProfile`. Adding a sport means adding one
module here and registering it — nothing in the agents or tools is
handball-specific.
"""

# Importing the module is what registers the profile, so this is not dead.
from sprtz_agents.sports import equestrian as _equestrian  # noqa: F401
from sprtz_agents.sports import handball as _handball  # noqa: F401
from sprtz_agents.sports.registry import (
    Discipline,
    MomentType,
    SportProfile,
    get_profile,
    list_sports,
    register_profile,
)

__all__ = [
    "Discipline",
    "MomentType",
    "SportProfile",
    "get_profile",
    "list_sports",
    "register_profile",
]

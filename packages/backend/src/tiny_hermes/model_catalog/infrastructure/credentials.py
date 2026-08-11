"""Read a model endpoint's credential from the deployment's environment.

The platform stores a name, never a value. `resolve` reads that name at call
time and hands back a string the caller puts straight into a header.

There is no wrapper class around the return value. One would be security
theatre: the string reaches an `Authorization` header either way, and a type
that pretends otherwise mostly succeeds at making people stop thinking about it.
What is enforced instead is that the value is never stored, never logged, and
never returned over any route — asserted by the tests around the routes that
could have leaked it.
"""

import os


class CredentialMissing(Exception):
    """The environment variable an endpoint names is not set in this process.

    Raised at registration as well as at call time, so a deployment that forgot
    to supply a key is found by the administrator who registered the endpoint
    rather than by whoever happened to submit the first Run.
    """

    def __init__(self, ref: str) -> None:
        super().__init__(f"the environment does not define {ref}")
        self.ref = ref


def resolve(ref: str) -> str:
    value = os.environ.get(ref)
    if not value:
        raise CredentialMissing(ref)
    return value


def is_available(ref: str) -> bool:
    return bool(os.environ.get(ref))

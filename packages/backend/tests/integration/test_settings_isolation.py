"""这个套件不许读开发者本机的 `.env`。

由来：`Settings` 的 `model_config` 写着 `env_file=".env"`，所以任何没有被
`conftest.py` 的 `settings` 夹具显式赋值的字段，都会落到仓库根那个 `.env` 上。
CI 上没有这个文件，本机通常有——于是同一份代码在两处跑出两种结果，而**本机那一份
是错的那一份**。

具体撞到的是 `model_catalog/test_endpoint_api.py` 的两条：它们的前提是「这个夹具的
平台没有配 egress 代理」，而 `.env` 里的 `EGRESS_PROXY_URL=http://egress-proxy:3128`
让那个前提不成立。症状是两条 FAILED，原因和被测代码毫无关系，每次跑全量都要有人
重新解释一遍「那两条是环境问题」——**而「那两条是环境问题」正是一句无法从输出里
验证的话**，它同样可以用来掩盖一个真的回归。

这里钉的是前提本身，不是那两条测试。
"""

from tiny_hermes.shared.config import Settings

from .conftest import BOOTSTRAP_TOKEN


def test_the_settings_fixture_does_not_read_the_repository_dot_env(
    settings: Settings,
) -> None:
    """夹具交出来的 `Settings` 必须和 CI 上的一样。

    断言 `egress_proxy_url` 而不是随便挑一个字段：它就是当初把两条测试染红的
    那一个，也是 `.env` 里确实有、而夹具没有显式赋值的那一个。换句话说，这条
    断言在修复之前是红的——它测的是真实发生过的那次污染。
    """
    # 按生产代码真正的判据断言，不是按 `is None`：`egress_proxy_url` 的默认值是
    # `""`，「没配」在 `api/cli.py:237` 和 `sandbox/cli.py:58` 里都写作
    # `not settings.egress_proxy_url`。断言 `is None` 会在字段默认值上先红一次，
    # 红的原因还和污染无关。
    assert not settings.egress_proxy_url


def test_a_settings_built_the_way_the_fixture_builds_one_carries_no_ambient_file(
    database_url: str, redis_url: str
) -> None:
    """同一件事，但绕开夹具自己造一个——防止将来有人在夹具里补一句
    `egress_proxy_url=None` 就让上面那条变绿，而 `.env` 照样在往别的字段里渗。

    传的是 `Settings` 必填的那几个（`s3_*` 也在其中——平时正是 `.env` 在供它们，
    这本身就说明这个文件渗进来多深）。其余留空：留空的字段如果拿到了非默认值，
    那个值只可能来自 `.env`。
    """
    built = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url=database_url,
        redis_url=redis_url,
        s3_endpoint="http://localhost:9000",
        s3_bucket="tiny-hermes-test",
        s3_access_key="tiny-hermes-local",
        s3_secret_key="tiny-hermes-local-password",
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token=BOOTSTRAP_TOKEN,
    )
    assert not built.egress_proxy_url
    assert not built.egress_proxy_token

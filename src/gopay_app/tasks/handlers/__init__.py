"""内置与业务任务 Handler。"""

from pathlib import Path

from gopay_app.protocols.legacy import LegacyProtocolAdapter
from gopay_app.services.sms_settings import HeroSmsSettingsStore, SmsSettingsStore

from ..registry import HandlerRegistry
from ..repository import TaskRepository
from .account_flow import AccountFlowHandler
from .builtin import echo_handler, sleep_handler, wait_input_handler
from .business import (
    AccountPinStatusHandler,
    AccountRefreshHandler,
    AccountRefreshSmsCodeHandler,
    AccountReleaseNumberHandler,
    SmsActivationCancelHandler,
)
from .payment import PaymentExecutionHandler, PaymentReconcileHandler
from .post_registration import AccountPostRegisterHandler


def build_default_registry(repository: TaskRepository, legacy_app_path: Path) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register("system.echo", echo_handler, description="回显队列载荷，用于运行检查")
    registry.register("system.sleep", sleep_handler, description="可取消的短时任务，用于并发检查")
    registry.register(
        "system.wait_input",
        wait_input_handler,
        description="演示 OTP 等一次性输入的持久化暂停与恢复",
    )
    adapter = LegacyProtocolAdapter(legacy_app_path)
    sms_store = SmsSettingsStore(repository.session_factory, repository.codec)
    hero_sms_store = HeroSmsSettingsStore(repository.session_factory, repository.codec)
    registry.register(
        "account.register",
        AccountFlowHandler(
            "register",
            repository.session_factory,
            repository.codec,
            adapter,
            sms_store,
            hero_sms_store,
        ),
        safe_to_retry=False,
        description="可恢复的 GoPay 注册、短信平台取码与 PIN 设置流程",
    )
    registry.register(
        "account.login",
        AccountFlowHandler(
            "login",
            repository.session_factory,
            repository.codec,
            adapter,
            sms_store,
            hero_sms_store,
        ),
        safe_to_retry=False,
        description="可恢复的已有账号登录、OTP 与 PIN 设置或修改流程",
    )
    registry.register(
        "account.post_register",
        AccountPostRegisterHandler(
            repository.session_factory,
            repository.codec,
            adapter,
        ),
        safe_to_retry=True,
        description="核心注册成功后的钱包激活、奖励余额等待与红包领取流程",
    )
    registry.register(
        "account.refresh",
        AccountRefreshHandler(repository.session_factory, repository.codec, adapter),
        safe_to_retry=True,
        description="刷新账号令牌与余额，并直接写入 SQLite",
    )
    registry.register(
        "account.check_pin",
        AccountPinStatusHandler(repository.session_factory, repository.codec, adapter),
        safe_to_retry=True,
        description="读取 GoPay 官方 PIN 配置状态并更新本地账号",
    )
    registry.register(
        "account.release_number",
        AccountReleaseNumberHandler(
            repository.session_factory,
            adapter,
            sms_store,
            hero_sms_store,
        ),
        safe_to_retry=True,
        description="释放账号关联的短信平台号码",
    )
    registry.register(
        "account.refresh_sms_code",
        AccountRefreshSmsCodeHandler(
            repository.session_factory,
            adapter,
            sms_store,
            hero_sms_store,
        ),
        safe_to_retry=False,
        description="忽略旧验证码并获取短信平台下一条最新验证码",
    )
    registry.register(
        "sms.cancel_activation",
        SmsActivationCancelHandler(
            repository.session_factory,
            adapter,
            sms_store,
            hero_sms_store,
        ),
        safe_to_retry=True,
        description="持久化延迟释放未使用的短信平台号码",
    )
    registry.register(
        "payment.execute",
        PaymentExecutionHandler(
            repository.session_factory,
            repository.codec,
            adapter,
            sms_store,
            hero_sms_store,
        ),
        safe_to_retry=False,
        description="可恢复的 Midtrans GoPay 绑定、OTP、PIN 与支付核验流程",
    )
    registry.register(
        "payment.reconcile",
        PaymentReconcileHandler(repository.session_factory, repository.codec, adapter),
        safe_to_retry=True,
        description="只读核验 Midtrans 远端交易状态，不重复执行扣款",
    )
    return registry


__all__ = ["build_default_registry"]

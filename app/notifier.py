"""
通知模块 — 多渠道通知发送

支持渠道：
  - Server酱 (serverchan)
  - PushPlus
  - 企业微信 (wecom)
  - 钉钉 (dingtalk)
"""
import time as _time
import hmac
import hashlib
import base64
from urllib.parse import quote_plus

from app.helpers import get_logger
from config.manager import load_config
from config.settings import http_client
import app.database as database

_log = get_logger()


class ServerChanNotifier:
    """Server酱通知"""

    def __init__(self, sckey):
        self.sckey = sckey

    def send(self, title, content):
        if not self.sckey:
            return False
        try:
            resp = http_client.post(
                f"https://sctapi.ftqq.com/{self.sckey}.send",
                data={"title": title, "desp": content},
            )
            return resp.json().get("code") == 0
        except Exception as e:
            _log.warning(f"Server酱通知发送失败: {e}")
            return False


class PushPlusNotifier:
    """PushPlus通知"""

    def __init__(self, token):
        self.token = token

    def send(self, title, content):
        if not self.token:
            return False
        try:
            resp = http_client.post(
                "https://www.pushplus.plus/send",
                json={"token": self.token, "title": title, "content": content},
            )
            return resp.json().get("code") == 200
        except Exception as e:
            _log.warning(f"PushPlus通知发送失败: {e}")
            return False


class WeComNotifier:
    """企业微信通知"""

    def __init__(self, webhook):
        self.webhook = webhook

    def send(self, title, content):
        if not self.webhook:
            return False
        try:
            resp = http_client.post(
                self.webhook,
                json={"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}},
            )
            return resp.json().get("errcode") == 0
        except Exception as e:
            _log.warning(f"企业微信通知发送失败: {e}")
            return False


class DingTalkNotifier:
    """钉钉通知"""

    def __init__(self, webhook, secret=""):
        self.webhook = webhook
        self.secret = secret

    def _sign_url(self):
        """生成带签名的钉钉 Webhook URL"""
        if not self.secret:
            return self.webhook
        timestamp = str(round(_time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, hashlib.sha256).digest()
        sign = quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook}&timestamp={timestamp}&sign={sign}"

    def send(self, title, content):
        if not self.webhook:
            return False
        try:
            url = self._sign_url()
            resp = http_client.post(
                url,
                json={"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}},
            )
            return resp.json().get("errcode") == 0
        except Exception as e:
            _log.warning(f"钉钉通知发送失败: {e}")
            return False


class NotificationService:
    """通知服务聚合器"""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.notifiers = {}
        notif = self.config.get("notifications", {})

        serverchan_cfg = notif.get("serverchan", {})
        if serverchan_cfg.get("enabled"):
            self.notifiers["serverchan"] = ServerChanNotifier(serverchan_cfg.get("sckey", ""))

        pushplus_cfg = notif.get("pushplus", {})
        if pushplus_cfg.get("enabled"):
            self.notifiers["pushplus"] = PushPlusNotifier(pushplus_cfg.get("token", ""))

        wecom_cfg = notif.get("wecom", {})
        if wecom_cfg.get("enabled"):
            self.notifiers["wecom"] = WeComNotifier(wecom_cfg.get("webhook", ""))

        dingtalk_cfg = notif.get("dingtalk", {})
        if dingtalk_cfg.get("enabled"):
            self.notifiers["dingtalk"] = DingTalkNotifier(
                dingtalk_cfg.get("webhook", ""), dingtalk_cfg.get("secret", ""),
            )

    def _log_notification(self, task_id, channel, title, content, success):
        """记录通知发送结果到数据库（send_report 和 send_message 共用）"""
        try:
            database.log_notification(
                task_id=task_id,
                channel=channel,
                title=title,
                content=content[:200],
                status='success' if success else 'failed',
                error_message=None if success else '发送失败',
            )
        except Exception as e:
            _log.warning(f"记录通知日志失败: {e}")

    def send_report(self, report, task_id=None):
        """发送简洁的扫描/下载报告"""
        title = "📋 标准抓取报告"
        content = self.format_report(report)

        results = {}
        for name, notifier in self.notifiers.items():
            success = notifier.send(title, content)
            results[name] = success
            self._log_notification(task_id, name, title, content, success)
        return results

    def send_message(self, title, content, task_id=None):
        """发送自定义消息"""
        results = {}
        for name, notifier in self.notifiers.items():
            success = notifier.send(title, content)
            results[name] = success
            self._log_notification(task_id, name, title, content, success)
        return results

    @staticmethod
    def format_report(report):
        """格式化报告为简洁消息"""
        lines = [
            f"📅 时间: {report.get('time', '')}",
            f"📊 扫描: {report.get('scanned', 0)} 条",
            f"⬇️ 下载: {report.get('downloaded', 0)} 条",
            f"✅ 成功: {report.get('success', 0)} 条",
            f"❌ 失败: {report.get('failed', 0)} 条",
            f"⏭️ 跳过: {report.get('skipped', 0)} 条",
        ]
        if "message" in report:
            lines.append(f"\n📝 {report['message']}")
        return "\n".join(lines)


_notification_service = None


def get_notification_service(force_reload=False):
    """获取通知服务实例（懒加载单例）"""
    global _notification_service
    if force_reload:
        _notification_service = None
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


def reset_notification_service():
    """重置通知服务实例"""
    global _notification_service
    _notification_service = None

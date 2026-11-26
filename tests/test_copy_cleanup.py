from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCES = (
    ROOT / "app.py",
    ROOT / "static" / "app.js",
    ROOT / "static" / "index.html",
)

REMOVED_COPY = (
    "人类玩家互相可见真实账号身份；裁判包仅含匿名代号。",
    "真人裁判自行注册并使用 UID 登录，再从大厅输入本房间邀请码，选择“真人裁判”身份加入。房主不创建账号，也不绑定已有账号。",
    "同一 UID 同时只能担任一个未完赛房间的真人裁判。裁判加入、出题和独立判断流程保持不变。",
    "实时同步中",
    "所有真人 / AI 裁判的问题会统一汇总；由房主在此开启作答。",
    "仍需完成全部轮次。等待所有裁判锁定最终判断。",
    "所有轮次已完成",
    "所有裁判均已锁定判断",
    "全部裁判已锁定",
    "仅本房间的人类参赛者、房主和管理员可见。真人裁判不参与此聊天。",
)


def test_removed_ui_copy_is_not_reintroduced():
    runtime_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in RUNTIME_SOURCES
    )
    reintroduced = [phrase for phrase in REMOVED_COPY if phrase in runtime_source]

    assert not reintroduced, (
        "Removed UI copy reappeared in runtime source: " + repr(reintroduced)
    )

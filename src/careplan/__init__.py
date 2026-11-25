"""社区居家上门照护的可承接计划工具包。

模块划分：
- timeutils   周锚定与日期解析
- sources     只依赖标准库的 CSV 读取与形状校验
- timeline    凭据事件溯源 -> 家庭逐周状态（占用/暂停保留/可腾退）
- capacity    站点班次、固定班、替代班与跨周采购
- history     历史团组的删失感知估计（劳时区间、续约风险、强退风险）
- backtest    只含当时已知凭据的滚动截点回验
- planning    可承诺余额、新一期审批、复用/替代登记、回看兑现
- registry    审批决定的重复登记与审计
- cli         命令行入口
"""

__all__ = [
    "timeutils",
    "sources",
    "timeline",
    "capacity",
    "history",
    "backtest",
    "planning",
    "registry",
]

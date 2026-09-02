"""会话滚动摘要。

compress_history 超预算时从最旧开始丢弃，被丢掉的内容就彻底消失了。原本的
理由是"敢丢的前提是记忆系统已经把该记的抽到 L2 了"——但这句话只在 L2 的判据
范围内成立。L2 存的是"用户明确说过的稳定事实"（不吃香菜、右肩有伤），而长
对话里丢掉的往往是另一类信息：

  - 这个计划当初是按什么前提排的（82kg、TDEE 2770、8 周）
  - 中途因为什么调整过（第三周说出差，把训练挪到了周末）
  - 助理已经解释过、不必重复的结论

这些既不是"稳定事实"（会随计划变，写进 L2 会污染记忆库并触发冲突消解），
也不是训练日志（L3 只存结构化记录），两条通道都不收。丢了之后模型会重新问
用户已经说过的话，或者忘记某个约束又提一遍被否掉的方案。

## 为什么是一张表而不是每轮现算

摘要必须**异步**生成。这条路径在用户等回复的主链路上，现算意味着每轮多一次
同步 LLM 调用——与项目"能异步的都挪到回复之后"的原则直接冲突，也和事实抽取、
待办抽取的处理方式不一致。

所以做成：worker 在回合结束后更新摘要，主链路只读已有的那份。代价是摘要总是
滞后一轮，但滞后的那一轮恰好还在 keep_recent 窗口里原样保留着，不构成缺口。

## covered_seq 是关键字段

它记录"摘要已经覆盖到哪条消息"。没有它就只能每次拿全部历史重新摘要一遍，
既浪费又会让摘要随调用次数漂移。有了它，每次只把新增的那几条增量并进去，
并且能让 build_context 精确判断：摘要覆盖范围与保留窗口之间是否真的存在缺口。
"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKey


class ConversationSummary(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "conversation_summaries"
    __table_args__ = (
        # 一个会话一份摘要。并发的两个回合可能同时想写，靠唯一约束 + upsert
        # 收敛，而不是靠"先查再插"——后者在并发下会插出两行。
        UniqueConstraint("conversation_id", name="uq_conversation_summaries_conv"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 摘要已覆盖到的最大 messages.seq。用它做增量，也用它判断摘要与保留窗口
    # 之间是否存在缺口。
    covered_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

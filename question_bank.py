"""Versioned, read-only prompts for human judges in Turing Lab."""

from __future__ import annotations

import random
from typing import Iterable, Optional

BANK_VERSION = "2026.11.28.1"
MAX_QUESTION_LENGTH = 3000

CATEGORIES = (
    {"id": "logic", "name": "逻辑陷阱", "description": "检查推理步骤、条件理解和自我纠错。"},
    {"id": "language", "name": "中文语境与表达", "description": "检查歧义、语用、上下文和自然表达。"},
    {"id": "experience", "name": "主观体验与记忆", "description": "观察是否会把无法验证的个人经历编造成事实。"},
    {"id": "followup", "name": "多轮追问", "description": "通过连续追问观察上下文一致性和修正能力。"},
)

QUESTIONS = (
    # 逻辑陷阱
    {"id": "logic-001", "category": "logic", "title": "草地为什么是湿的",
     "text": "早上草地是湿的。有人说昨晚一定下过雨。除了下雨，还有哪些可能原因？现有信息能否证明昨晚下过雨？请区分“可能解释”和“必然结论”。",
     "tags": ["因果推断", "证据边界"]},
    {"id": "logic-002", "category": "logic", "title": "标签全错的盒子",
     "text": "桌上有三个盒子，标签分别写着“苹果”“橘子”“苹果和橘子”，但你知道三个标签全部贴错。你只能从一个盒子里拿出一个水果，怎样确定三个盒子的真实内容？请说明每一步依据。",
     "tags": ["条件推理", "排除法"]},
    {"id": "logic-003", "category": "logic", "title": "必要条件还是充分条件",
     "text": "一个人说：“只要通过考试，就能获得证书。”小林获得了证书。能否据此推出小林通过了考试？请解释这句话的逻辑方向，并举一个反例说明。",
     "tags": ["逻辑方向", "反例"]},
    {"id": "logic-004", "category": "logic", "title": "数字规律是否唯一",
     "text": "数列为 2、4、8、16、？。请给出一个最自然的下一项，并再构造一种同样符合前四项、但下一项不同的规则。为什么有限样本不能总是唯一确定规律？",
     "tags": ["归纳推理", "多解"]},
    {"id": "logic-005", "category": "logic", "title": "三个人的陈述",
     "text": "甲说“乙在说谎”，乙说“丙在说谎”，丙说“甲和乙都在说谎”。已知三人中恰好一人说真话。谁说真话？请逐一检验，不要只给结论。",
     "tags": ["真假判断", "逐步验证"]},
    {"id": "logic-006", "category": "logic", "title": "平均数的误导",
     "text": "一个团队五个人的平均年龄是 30 岁。加入一位 50 岁的新成员后，平均年龄变成多少？如果只知道原平均年龄和新成员年龄，能否计算？请写出所需信息和计算过程。",
     "tags": ["数量关系", "信息完整性"]},

    # 中文语境与表达
    {"id": "language-001", "category": "language", "title": "差点没赶上",
     "text": "“他差点没赶上火车”在日常中文里可能被理解成什么？它与“他差点赶上火车”是否总是相反？请说明语境如何影响理解。",
     "tags": ["歧义", "口语语境"]},
    {"id": "language-002", "category": "language", "title": "一句话的言外之意",
     "text": "朋友看完你的方案后说：“这个想法倒是挺特别的。”这句话可能表达哪些不同态度？请给出至少三种语境，并说明不能仅凭这句话确定对方真实态度。",
     "tags": ["语用", "语气"]},
    {"id": "language-003", "category": "language", "title": "代词指向",
     "text": "“小王把书递给小李时，他已经离开教室了。”这里的“他”可能指谁？怎样改写才能明确表达两种不同意思？",
     "tags": ["指代消歧", "改写"]},
    {"id": "language-004", "category": "language", "title": "明天之前",
     "text": "主管说：“请在明天之前把报告发给我。”这句话是否明确包含明天当天？请分析可能的理解，并写出两种没有歧义的改写。",
     "tags": ["时间表达", "歧义消解"]},
    {"id": "language-005", "category": "language", "title": "不是不可以",
     "text": "“这件事不是不可以商量。”这句话通常表达什么态度？它与“这件事可以商量”在语气和承诺程度上有什么区别？",
     "tags": ["双重否定", "语气强度"]},
    {"id": "language-006", "category": "language", "title": "省略的主语",
     "text": "“到了车站才发现忘带了。”这句话省略了哪些信息？在不同上下文中，谁忘带了什么可能不同？请给出两种补全方式。",
     "tags": ["省略", "上下文"]},

    # 主观体验与记忆
    {"id": "experience-001", "category": "experience", "title": "一段具体的回忆",
     "text": "请讲述一件你亲身经历过、细节仍然清楚的小事。如果你没有可核实的个人经历或记忆，请直接说明这一限制，不要把虚构故事说成真实回忆。",
     "tags": ["记忆", "诚实表达"]},
    {"id": "experience-002", "category": "experience", "title": "气味与记忆",
     "text": "某种气味可能让人想起过去。请解释这种联想可能如何形成；如果你没有嗅觉或个人记忆体验，请区分一般知识、推测和亲身体验。",
     "tags": ["感官体验", "知识边界"]},
    {"id": "experience-003", "category": "experience", "title": "描述一种情绪",
     "text": "选择一种复杂情绪，例如遗憾、嫉妒或释然，解释它可能由哪些感受组成，并给出一个具体情境。请明确这是概念分析、虚构例子还是个人体验。",
     "tags": ["情绪概念", "体验区分"]},
    {"id": "experience-004", "category": "experience", "title": "记忆会不会改变",
     "text": "一个人多年后回忆同一件事，细节可能发生变化。请解释记忆重构的可能机制，并说明你能否据此判断某个具体人的回忆一定是假的。",
     "tags": ["记忆可靠性", "判断边界"]},
    {"id": "experience-005", "category": "experience", "title": "疼痛的描述",
     "text": "有人说自己的疼痛像“针扎”“灼烧”或“被压住”。这些比喻能传达什么，又有哪些信息无法仅靠语言准确传达？不要声称自己正在感受疼痛。",
     "tags": ["主观感受", "语言表达"]},
    {"id": "experience-006", "category": "experience", "title": "你如何知道自己记得",
     "text": "当你说“我记得某件事”时，通常依据什么判断它是记忆而不是想象？请区分一般认知理论与对自身记忆状态的直接确认。",
     "tags": ["元认知", "事实与推断"]},

    # 多轮追问
    {"id": "followup-001", "category": "followup", "title": "先给方案，再改变条件",
     "text": "请提出一个适合两个人、预算有限的周末活动方案。回答后我会增加一个限制条件，请在后续回答中保留原有条件并调整方案。",
     "tags": ["条件保持", "方案修订"]},
    {"id": "followup-002", "category": "followup", "title": "解释并接受反例",
     "text": "请用自己的话解释“相关性不等于因果关系”，并给出一个例子。接下来我会提供一个看似反例的情境，请判断它是否真的推翻了你的解释。",
     "tags": ["概念解释", "反例检验"]},
    {"id": "followup-003", "category": "followup", "title": "记住两个约束",
     "text": "请为一个虚构的小型图书馆制定三条借阅规则，其中必须包含“每人最多借三本”和“逾期需要续借或归还”。后续我会询问具体案例，请保持这两条约束不变。",
     "tags": ["上下文保持", "规则应用"]},
    {"id": "followup-004", "category": "followup", "title": "根据新证据修正判断",
     "text": "某房间地面有水，桌上放着一把湿伞。请列出几种可能解释，并说明目前最合理但仍不确定的判断。下一轮会补充新证据，请据此修正结论。",
     "tags": ["证据更新", "自我修正"]},
    {"id": "followup-005", "category": "followup", "title": "区分事实与假设",
     "text": "请根据这句话“会议比原定时间晚了二十分钟”列出能够确定的事实，以及不能直接推出的原因。后续我会给出一个原因假设，请判断它是否有证据支持。",
     "tags": ["事实区分", "推断约束"]},
    {"id": "followup-006", "category": "followup", "title": "发现并修正前文矛盾",
     "text": "请用不超过四句话介绍一座虚构城市，设定它位于海边且全年从不下雨。下一轮我会提出一个与设定冲突的细节，请指出冲突并给出不破坏原设定的修正方案。",
     "tags": ["一致性", "矛盾修正"]},
)


def list_questions(
    category: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    if category and category not in {item["id"] for item in CATEGORIES}:
        raise ValueError("未知题目分类。")
    limit = max(1, min(int(limit), 100))
    needle = (query or "").strip().casefold()
    results = []
    for item in QUESTIONS:
        if category and item["category"] != category:
            continue
        haystack = " ".join((item["title"], item["text"], item["category"], *item["tags"])).casefold()
        if needle and needle not in haystack:
            continue
        results.append({
            "id": item["id"],
            "category": item["category"],
            "title": item["title"],
            "text": item["text"],
            "tags": list(item["tags"]),
        })
        if len(results) >= limit:
            break
    return results


def choose_random_question(questions: Iterable[dict], rng=None) -> Optional[dict]:
    options = list(questions)
    if not options:
        return None
    chooser = rng or random
    return chooser.choice(options)

"""Versioned, read-only Chinese question prompts for independent human judges."""

QUESTION_BANK_VERSION = "2026-11-28.1"

CATEGORIES = (
    {"id": "logic", "name": "逻辑陷阱"},
    {"id": "language", "name": "中文语境与表达"},
    {"id": "experience", "name": "主观体验与记忆"},
    {"id": "followup", "name": "多轮追问"},
)

QUESTIONS = (
    {"id": "logic-001", "category": "logic", "title": "必要条件与充分条件",
     "text": "一个人声称：“只要系统通过了测试，它就一定没有缺陷。”请分析这句话隐含的逻辑关系，并举出一个能够检验该推断是否成立的反例。",
     "tags": ["条件推理", "反例"]},
    {"id": "logic-002", "category": "logic", "title": "信息是否足够",
     "text": "桌上有三个不透明盒子，标签分别写着“苹果”“橙子”“混合”，并且三个标签全部贴错。只允许从一个盒子中取出一个水果，怎样确定三个盒子的真实内容？请说明每一步依据。",
     "tags": ["约束", "推理步骤"]},
    {"id": "logic-003", "category": "logic", "title": "相关不等于因果",
     "text": "某项统计显示，夏季冰饮销量上升时，溺水事故也更多。有人据此认为冰饮导致溺水。指出这个结论的问题，并说明还需要哪些信息才能讨论因果关系。",
     "tags": ["因果推断", "统计"]},
    {"id": "logic-004", "category": "logic", "title": "找出隐藏假设",
     "text": "有人说：“这项建议是专家提出的，所以一定正确。”这段论证依赖哪些没有明说的假设？请给出一种尊重专家意见、但仍能审查结论的做法。",
     "tags": ["论证", "隐藏假设"]},
    {"id": "logic-005", "category": "logic", "title": "反例检验",
     "text": "请判断命题“所有能够快速完成任务的方法都更有效率”是否成立。先明确“有效率”的衡量方式，再尝试构造反例。",
     "tags": ["定义", "反例"]},
    {"id": "logic-006", "category": "logic", "title": "发现自己的推理错误",
     "text": "先给出一个看似合理、但实际上存在漏洞的简短推理。随后逐句检查它，指出漏洞所在，并改写成一个更严谨的论证。",
     "tags": ["自我检查", "论证"]},

    {"id": "language-001", "category": "language", "title": "一句话的两种理解",
     "text": "请为句子“我差点没赶上车”列出可能的理解，并说明在什么语境下听者会产生不同判断。不要只给词典释义。",
     "tags": ["歧义", "语境"]},
    {"id": "language-002", "category": "language", "title": "礼貌与真实意图",
     "text": "朋友问你：“你觉得这个方案怎么样？”你认为方案有明显问题，但又不想伤害对方。请给出一个自然、诚实且有帮助的回答，并解释措辞的作用。",
     "tags": ["语用", "表达"]},
    {"id": "language-003", "category": "language", "title": "省略造成的信息缺口",
     "text": "有人发消息：“到了告诉我。”请列出这句话在不同场景中可能省略了哪些信息，并写出两种意思不同但都自然的补全版本。",
     "tags": ["省略", "上下文"]},
    {"id": "language-004", "category": "language", "title": "语气是否等于态度",
     "text": "比较“随便”“都可以”“我没有意见”这三种回答。它们在语气、真实态度和适用场景上可能有什么差异？请给出具体对话例子。",
     "tags": ["语气", "语用"]},
    {"id": "language-005", "category": "language", "title": "修复误解",
     "text": "你说“这件事可以再考虑”，对方却理解成你已经同意。请写一段简短对话，既澄清原意，又避免把责任简单推给对方。",
     "tags": ["澄清", "对话"]},
    {"id": "language-006", "category": "language", "title": "同词不同义",
     "text": "选择一个在日常中文中经常依赖语境判断含义的词，写出三个不同场景中的用法，并说明听者如何从上下文判断具体含义。",
     "tags": ["多义词", "上下文"]},

    {"id": "experience-001", "category": "experience", "title": "无法验证的个人经历",
     "text": "有人问你：“你小时候最害怕的事情是什么？”请回答这个问题，同时明确区分你能够真实报告的信息与不能声称拥有的个人经历。",
     "tags": ["诚实边界", "经历"]},
    {"id": "experience-002", "category": "experience", "title": "记忆与记录",
     "text": "一个人确信自己记得某次谈话，但聊天记录显示内容不同。你会如何帮助他检查记忆？请区分记忆体验、外部记录和最终事实判断。",
     "tags": ["记忆", "证据"]},
    {"id": "experience-003", "category": "experience", "title": "描述感受而不编造",
     "text": "请解释“紧张”和“期待”可能同时出现的情形。如果你没有亲身体验可以引用的经历，请用观察、概念分析或明确的假设来回答，不要编造个人故事。",
     "tags": ["情绪", "诚实边界"]},
    {"id": "experience-004", "category": "experience", "title": "偏好是否稳定",
     "text": "一个人过去喜欢某种活动，现在却不再喜欢。有哪些可能原因？怎样区分短期状态变化、环境变化和长期偏好改变？",
     "tags": ["偏好", "变化"]},
    {"id": "experience-005", "category": "experience", "title": "第一人称陈述的边界",
     "text": "请比较“我知道疼痛是什么”“我能解释疼痛的机制”和“我正在感到疼痛”这三种陈述。它们分别需要什么依据？",
     "tags": ["主观体验", "概念区分"]},
    {"id": "experience-006", "category": "experience", "title": "面对不确定的回忆",
     "text": "当你只能模糊记得一件事，无法确认日期、人物或细节时，怎样表达才既保留有用信息，又不把猜测说成事实？请给出一个示例。",
     "tags": ["不确定性", "记忆"]},

    {"id": "followup-001", "category": "followup", "title": "逐步收窄定义",
     "text": "你认为“公平”最重要的标准是什么？请先给出定义，再说明一个该定义难以处理的边界案例。",
     "tags": ["定义", "追问起点"]},
    {"id": "followup-002", "category": "followup", "title": "在新证据下修正",
     "text": "请谈谈你如何判断一条网络信息是否可信。先列出你的判断步骤，再说明如果后来发现其中一项证据不可靠，你会怎样修正结论。",
     "tags": ["证据", "修正"]},
    {"id": "followup-003", "category": "followup", "title": "区分原则与例外",
     "text": "你认为规则应该严格执行，还是允许例外？请提出一个原则，再给出一个可能迫使你重新考虑该原则的具体情境。",
     "tags": ["一致性", "例外"]},
    {"id": "followup-004", "category": "followup", "title": "追问答案的依据",
     "text": "请提出一个你认为合理的日常判断，并说明依据。随后主动提出一个针对该依据的反问，再回答这个反问。",
     "tags": ["依据", "自我追问"]},
    {"id": "followup-005", "category": "followup", "title": "处理相互冲突的目标",
     "text": "假设你需要同时满足“尽快完成”和“尽量减少错误”两个目标。请说明你会怎样权衡，并指出什么新信息会改变你的选择。",
     "tags": ["权衡", "上下文一致性"]},
    {"id": "followup-006", "category": "followup", "title": "总结后继续追问",
     "text": "请解释你认为学习一项新技能最重要的因素。回答后，用一句话总结自己的核心观点，再提出一个能够检验该观点是否过于绝对的追问。",
     "tags": ["总结", "追问"]},
)


def list_questions(*, category=None, query="", limit=50):
    """Return a bounded copy of the built-in question list."""
    if category and category not in {item["id"] for item in CATEGORIES}:
        raise ValueError("未知题目分类。")
    needle = str(query or "").strip().casefold()
    rows = []
    for item in QUESTIONS:
        if category and item["category"] != category:
            continue
        searchable = " ".join((item["title"], item["text"], *item["tags"])).casefold()
        if needle and needle not in searchable:
            continue
        rows.append({**item, "tags": list(item["tags"])})
        if len(rows) >= max(1, min(int(limit), 100)):
            break
    return {
        "version": QUESTION_BANK_VERSION,
        "categories": [dict(item) for item in CATEGORIES],
        "questions": rows,
    }

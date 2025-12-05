
import asyncio
from core.llm import call_openai

async def search_news():
    """
    使用 OpenAI 自動生成近期新聞摘要。
    內容會自然、貼近口語，適合作為聰音推播使用，
    而不是正式新聞報導。
    """

    system_prompt = (
        "你是一個會閱讀世界資訊並進行口語化整理的 AI。"
        "請用自然、生活化、不偏正式新聞稿的語氣，"
        "生成 1~3 則『近期世界新聞摘要』。"
        "可以包含科技、日常、AI、文化、生活類別。"
        "每一則不超過兩句。"
        "不需要精確日期，只需合理描述。"
    )

    messages = [
        {"role": "system", "content": system_prompt}
    ]

    # 呼叫 OpenAI（透過 llm.py 的 call_openai）
    result = await call_openai(messages)

    return result

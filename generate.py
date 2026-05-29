import os
import re
import html
import time
import json
import logging
import uuid
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ==========================================
# 1. ログ・フォルダ初期設定
# ==========================================
os.makedirs("logs", exist_ok=True)
os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    filename="logs/app.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

MAX_ARTICLES_LIMIT = 30
MAX_HISTORY_LIMIT = 5000

# ==========================================
# 2. Pydanticスキーマ定義
# ==========================================
class ArticleOutputSchema(BaseModel):
    title: str = Field(description="日本語のキャッチーで分かりやすいタイトル。〜が登場、〜が可能に、など動詞で終わる自然な表現（35文字以内）。")
    summary_1: str = Field(description="3行結論の1つ目。事実のみで構成され、推測や感想を含めない。体言止めで30文字以内。")
    summary_2: str = Field(description="3行結論の2つ目。事実のみで構成され、推測や感想を含めない。体言止めで30文字以内。")
    summary_3: str = Field(description="3行結論の3つ目。事実のみで構成され、推測や感想を含めない。体言止めで30文字以内。")
    summary_detail: str = Field(description="3行まとめのさらに詳しい解説。中級者向けの客観的な技術詳細。150文字程度。")
    explanation_intro: str = Field(description="初心者向け解説の導入。興味を惹く一文。50文字以内。")
    explanation_full: str = Field(description="初心者向け解説の続き。「たとえば〜」から始まる具体的な比喩を必ず含め、専門用語を使わずに中学生でも理解できるように優しく噛み砕いた解説。150文字程度。")
    action_1: str = Field(description="一般ユーザーや日本のビジネスマンへの具体的な影響や実践的な実用例。")
    action_2: str = Field(description="一般ユーザーや初心者が「まず今すぐ試すべきアクション」の具体的な推奨。")
    slug: str = Field(description="保存するファイル名に使用する半角英数字とハイフンのみのスラグ。例: 'claude-4-release'")

# ==========================================
# 3. スラグのサニタイズ
# ==========================================
def sanitize_slug(raw_slug: str) -> str:
    slug = re.sub(r'[^a-z0-9\-]', '', raw_slug.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        slug = f"article-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return slug[:80]

# ==========================================
# 4. 履歴管理
# ==========================================
HISTORY_FILE = "logs/history.json"

def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            converted_history = []
            for item in raw_data:
                if isinstance(item, str):
                    converted_history.append({
                        "url": item,
                        "processed_at": datetime.now().isoformat(),
                        "status": "published"
                    })
                elif isinstance(item, dict) and "url" in item:
                    converted_history.append(item)
            return converted_history
        except Exception as e:
            logging.error(f"履歴ファイルの読み込みに失敗しました（初期化します）: {e}")
    return []

def save_history(history: list):
    try:
        trimmed_history = history[-MAX_HISTORY_LIMIT:]
        tmp_history_file = HISTORY_FILE + ".tmp"
        with open(tmp_history_file, "w", encoding="utf-8") as f:
            json.dump(trimmed_history, f, ensure_ascii=False, indent=2)
        os.replace(tmp_history_file, HISTORY_FILE)
    except Exception as e:
        logging.error(f"履歴ファイルの保存に失敗しました: {e}")

# ==========================================
# 5. RSS取得・パース
# ==========================================
def fetch_rss_feed(rss_url: str) -> list:
    articles = []
    try:
        logging.info(f"RSSフィードを取得中: {rss_url}")
        req = urllib.request.Request(
            rss_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/rss+xml, application/xml, text/xml, */*'
            }
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()

        root = ET.fromstring(xml_data)

        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else ""
            link = item.find('link').text if item.find('link') is not None else ""
            description = item.find('description').text if item.find('description') is not None else ""
            articles.append({"title": title, "link": link, "description": description})

        if not articles:
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            for entry in root.findall('.//atom:entry', ns):
                title = entry.find('atom:title', ns).text if entry.find('atom:title', ns) is not None else ""
                link_elem = entry.find('atom:link', ns)
                link = link_elem.get('href') if link_elem is not None else ""
                summary = entry.find('atom:summary', ns).text if entry.find('atom:summary', ns) is not None else ""
                articles.append({"title": title, "link": link, "description": summary})

    except Exception as e:
        logging.error(f"RSSの取得またはパースに失敗しました ({rss_url}): {e}")

    return articles

# ==========================================
# 6. コア：AI要約 & HTML生成
# ==========================================
def run_article_generator(source_text: str, source_url: str, source_name: str) -> str:
    MAX_INPUT_LENGTH = 12000
    safe_source_text = source_text[:MAX_INPUT_LENGTH]

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.error("環境変数 'GEMINI_API_KEY' が設定されていません。")
        return ""

    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    prompt = f"""
    あなたは、「AI初心者でも直感的に理解できる」日本語コンテンツを作成する、日本最高レベルのAIニュース編集者です。
    以下の【厳守ルール】に厳密に従い、海外AI記事の日本語コンテンツを生成してください。

    【厳守ルール】
    - 専門用語を限界まで噛み砕き、中学生でもイメージできる平易な日本語にしてください。
    - 誇張を排し、客観的かつ断定しすぎない知的なトーンを保ってください。
    - titleは日本のエンジニアやビジネスマンがクリックしたくなる自然な表現にしてください。
    - slugは半角英数字とハイフンのみで、英単語3〜5語程度（例: gemini-2-flash-release）で指定してください。

    【元記事（英語）】
    {safe_source_text}
    """

    MAX_RETRIES = 3
    response_text = ""

    for attempt in range(MAX_RETRIES):
        try:
            logging.info(f"Gemini API呼び出し中 (試行 {attempt + 1}/{MAX_RETRIES})...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ArticleOutputSchema,
                    http_options=types.HttpOptions(timeout=60000)
                )
            )
            if response and response.text:
                response_text = response.text
                break
            else:
                raise ValueError("APIレスポンスのテキストが空でした。")
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                wait = 2 ** attempt
                logging.warning(f"APIレート制限（429）。{wait}秒待機してリトライ...")
                time.sleep(wait)
            elif "INVALID_ARGUMENT" in err:
                logging.error(f"プロンプト不正（回復不能なエラー）: {e}")
                return ""
            else:
                wait = 2 ** attempt
                logging.warning(f"一時的なAPI接続失敗（試行 {attempt + 1}）: {err}")
                time.sleep(wait)
    else:
        logging.error("最大リトライ回数を超えたため、生成を中止しました。")
        return ""

    response_text = response_text.strip()
    response_text = re.sub(r"^```json\s*|\s*```$", "", response_text, flags=re.IGNORECASE).strip()

    if not response_text.startswith("{"):
        logging.error(f"Geminiの出力がJSON形式ではありません: {response_text[:200]}")
        return ""

    try:
        data = json.loads(response_text)
        validated_data = ArticleOutputSchema(**data)
    except Exception as e:
        logging.error(f"バリデーション失敗: {e}")
        return ""

    article_dict = validated_data.model_dump()
    slug = sanitize_slug(article_dict["slug"])

    output_html_path = os.path.join("articles", f"{slug}.html")
    if os.path.exists(output_html_path):
        suffix = uuid.uuid4().hex[:8]
        slug = f"{slug}-{suffix}"
        output_html_path = os.path.join("articles", f"{slug}.html")
        article_dict["slug"] = slug

    output_json_path = os.path.join("data", f"{slug}.json")

    safe_data = {k: html.escape(str(v)) for k, v in article_dict.items() if k != "slug"}
    safe_source_url = html.escape(source_url)
    safe_source_name = html.escape(source_name)

    now = datetime.now()
    date_iso = now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    date_ja = now.strftime("%Y年%m月%d日 %H:%M")

    template_path = "template_article.html"
    if not os.path.exists(template_path):
        logging.error(f"テンプレート '{template_path}' が見つかりません。")
        return ""

    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    replacements = {
        "{{TITLE}}": safe_data["title"],
        "{{DATE_ISO}}": date_iso,
        "{{DATE_JA}}": date_ja,
        "{{SOURCE_URL}}": safe_source_url,
        "{{SOURCE_NAME}}": safe_source_name,
        "{{SUMMARY_1}}": safe_data["summary_1"],
        "{{SUMMARY_2}}": safe_data["summary_2"],
        "{{SUMMARY_3}}": safe_data["summary_3"],
        "{{SUMMARY_DETAIL}}": safe_data["summary_detail"],
        "{{EXPLANATION_INTRO}}": safe_data["explanation_intro"],
        "{{EXPLANATION_FULL}}": safe_data["explanation_full"],
        "{{ACTION_1}}": safe_data["action_1"],
        "{{ACTION_2}}": safe_data["action_2"]
    }

    html_content = template_content
    for placeholder, value in replacements.items():
        html_content = html_content.replace(placeholder, value)

    if "{{" in html_content:
        logging.warning("警告：テンプレート内に未置換の変数が残っている可能性があります。")

    try:
        tmp_html_path = output_html_path + ".tmp"
        tmp_json_path = output_json_path + ".tmp"

        with open(tmp_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        os.replace(tmp_html_path, output_html_path)

        with open(tmp_json_path, "w", encoding="utf-8") as f:
            json.dump(article_dict, f, ensure_ascii=False, indent=2)
        os.replace(tmp_json_path, output_json_path)

        logging.info(f"記事生成成功: {slug}")
        return slug
    except Exception as e:
        logging.error(f"ファイル書き込み失敗: {e}")
        return ""

# ==========================================
# 7. template_index.html を基にした index.html 完全自動コンパイル（SSG方式）
# ==========================================
def rebuild_index_and_rotate_storage():
    """template_index.html を読み込み、最新記事を流し込んで index.html を全自動生成する"""
    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json")]
        all_articles = []

        for j_file in json_files:
            path = os.path.join("data", j_file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    article_data = json.load(f)
                mtime = os.path.getmtime(path)
                all_articles.append((mtime, article_data))
            except Exception as e:
                logging.error(f"JSON読み込み失敗 ({j_file}): {e}")

        # 最新順にソート（更新時間が新しい順）
        all_articles.sort(key=lambda x: x[0], reverse=True)

        # 容量パンク防止のローテーション物理削除
        if len(all_articles) > MAX_ARTICLES_LIMIT:
            logging.info(f"記事数が上限（{MAX_ARTICLES_LIMIT}件）を超えたため、古いファイルを自動削除します。")
            to_delete = all_articles[MAX_ARTICLES_LIMIT:]
            all_articles = all_articles[:MAX_ARTICLES_LIMIT]
            for _, d_art in to_delete:
                d_slug = sanitize_slug(d_art["slug"])
                for path in [
                    os.path.join("articles", f"{d_slug}.html"),
                    os.path.join("data", f"{d_slug}.json")
                ]:
                    if os.path.exists(path):
                        os.remove(path)
                logging.info(f"古い記事を削除しました: {d_slug}")

        # 🔴 改善：template_index.html を安全に読み込んでビルドする
        template_index_path = "template_index.html"
        if not os.path.exists(template_index_path):
            logging.error("template_index.html が見つかりません。")
            return

        with open(template_index_path, "r", encoding="utf-8") as f:
            index_template_content = f.read()

        # もし記事データがまだ1件も生成されていない場合、デフォルト表示にする
        if not all_articles:
            logging.info("データフォルダが空のため、一覧の更新を保留します。")
            return

        # 1. ヒーロー記事（最新の1位）のデータを取得してエスケープ
        _, hero_art = all_articles[0]
        safe_hero_title = html.escape(hero_art["title"])
        safe_hero_sum1 = html.escape(hero_art["summary_1"])
        safe_hero_sum2 = html.escape(hero_art["summary_2"])
        safe_hero_sum3 = html.escape(hero_art["summary_3"])
        safe_hero_detail = html.escape(hero_art["summary_detail"])
        safe_hero_intro = html.escape(hero_art["explanation_intro"])
        safe_hero_full = html.escape(hero_art["explanation_full"])
        safe_hero_action1 = html.escape(hero_art["action_1"])
        safe_hero_action2 = html.escape(hero_art["action_2"])
        safe_hero_url = html.escape(hero_art.get("source_url", "#"))
        safe_hero_name = html.escape(hero_art.get("source_name", "ソース"))
        
        hero_date_ja = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        hero_date_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

        # 2. 2番目以降の古い記事（2位〜最大30位）をグリッド用のカードに変換
        grid_articles = all_articles[1:]
        articles_html = ""
        for _, art in grid_articles:
            safe_title = html.escape(art["title"])
            safe_intro = html.escape(art["explanation_intro"])
            safe_slug = sanitize_slug(art["slug"])
            
            articles_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span>AI News</span>
                        <span>Latest Release</span>
                    </div>
                    <h3>{safe_title}</h3>
                    <p>{safe_intro}</p>
                    <a href="articles/{safe_slug}.html">続きを読む &rarr;</a>
                </article>
            """

        # 🔴 改善：template_index.html のすべての変数を、安全に一括置換！
        index_content = index_template_content
        index_content = index_content.replace("{{TITLE}}", safe_hero_title)
        index_content = index_content.replace("{{DATE_ISO}}", hero_date_iso)
        index_content = index_content.replace("{{DATE_JA}}", hero_date_ja)
        index_content = index_content.replace("{{SOURCE_URL}}", safe_hero_url)
        index_content = index_content.replace("{{SOURCE_NAME}}", safe_hero_name)
        index_content = index_content.replace("{{SUMMARY_1}}", safe_hero_sum1)
        index_content = index_content.replace("{{SUMMARY_2}}", safe_hero_sum2)
        index_content = index_content.replace("{{SUMMARY_3}}", safe_hero_sum3)
        index_content = index_content.replace("{{SUMMARY_DETAIL}}", safe_hero_detail)
        index_content = index_content.replace("{{EXPLANATION_INTRO}}", safe_hero_intro)
        index_content = index_content.replace("{{EXPLANATION_FULL}}", safe_hero_full)
        index_content = index_content.replace("{{ACTION_1}}", safe_hero_action1)
        index_content = index_content.replace("{{ACTION_2}}", safe_hero_action2)
        index_content = index_content.replace("{{ARTICLES_GRID}}", articles_html)

        # 原子性を維持した書き出し保存（index.htmlを作成）
        index_path = "index.html"
        tmp_index_path = index_path + ".tmp"
        with open(tmp_index_path, "w", encoding="utf-8") as f:
            f.write(index_content)
        os.replace(tmp_index_path, index_path)

        logging.info("template_index.html から index.html を全自動生成（ビルド）しました。")
        print("✅ index.html の全自動ビルドおよびローテーション削除が完了しました！")

    except Exception as e:
        logging.error(f"index.html の全自動ビルド中にエラーが発生しました: {e}")

# ==========================================
# 8. オーケストレーター
# ==========================================
def main():
    RSS_FEEDS = [
        {"url": "https://blog.google/technology/ai/rss/", "name": "Google AI Blog"},
        {"url": "https://openai.com/news/rss.xml", "name": "OpenAI Blog"},
    ]

    logging.info("--- 自動巡回タスクを開始します ---")
    history = load_history()
    processed_urls = {h["url"] for h in history if isinstance(h, dict) and "url" in h}

    new_article_created = False
    
    # 🔴 改善：初回起動時にデータ（data/）がまだ空の場合のみ、挙動テストのためにGoogle AIブログの模擬データから
    # 強制的に1記事を生成するお助けテスト機能を搭載。
    data_files = [f for f in os.listdir("data") if f.endswith(".json")]
    if not data_files:
        logging.info("データフォルダが空のため、初期挙動テスト用にデモ記事を自動生成します。")
        print("💡 初期データを検出。初回テスト用の要約を自動生成しています...")
        mock_source_text = """
        Google has introduced Gemini 2.5, a massive upgrade in generative AI capabilities.
        The new model offers exceptional processing speeds and deeply integrated multimodality.
        Users can now analyze real-time video streams and complex codebases seamlessly.
        """
        slug = run_article_generator(
            source_text=mock_source_text,
            source_url="https://blog.google/technology/ai/",
            source_name="Google AI Official"
        )
        if slug:
            new_article_created = True

    # RSSの通常監視
    MAX_PROCESS_PER_RUN = 1
    processed_count = 0

    for feed in RSS_FEEDS:
        if processed_count >= MAX_PROCESS_PER_RUN:
            break

        fetched_articles = fetch_rss_feed(feed["url"])
        if not fetched_articles:
            continue

        for item in fetched_articles:
            if processed_count >= MAX_PROCESS_PER_RUN:
                break

            if item["link"] in processed_urls:
                continue

            if not item["description"] or len(item["description"]) < 100:
                logging.info(f"descriptionが短すぎるためスキップ: {item['title']}")
                history.append({
                    "url": item["link"],
                    "processed_at": datetime.now().isoformat(),
                    "status": "skipped",
                    "reason": "description_too_short"
                })
                processed_urls.add(item["link"])
                continue

            logging.info(f"未処理の新着記事を検知（{feed['name']}）: {item['title']}")
            print(f"📡 新着記事を検知: {item['title']}")

            slug = run_article_generator(
                source_text=item["description"],
                source_url=item["link"],
                source_name=feed["name"]
            )

            if slug:
                history.append({
                    "url": item["link"],
                    "processed_at": datetime.now().isoformat(),
                    "status": "published"
                })
                processed_count += 1
                new_article_created = True
                time.sleep(5)

    save_history(history)

    # 🔴 改善：手動のtemplate_index.html変更やテンプレートの変更がいつでも即時反映されるよう、
    # 新着記事の有無に関わらず、起動されたら必ず最新データを基に「再ビルド」を100%実行する仕様にアップグレード！
    rebuild_index_and_rotate_storage()

if __name__ == "__main__":
    main()

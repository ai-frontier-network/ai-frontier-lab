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
import socket
socket.setdefaulttimeout(30)  # 30秒間無反応なら自動でタイムアウトを発生させ、リトライ処理に移行させます

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
TEMPLATE_VERSION = "1.5.0"

# ==========================================
# 2. Pydanticスキーマ定義（JSONの完全バリデーション）
# ==========================================
class ArticleOutputSchema(BaseModel):
    title: str = Field(description="日本語のキャッチーで分かりやすいタイトル。〜が登場、〜が可能に、など動詞で終わる自然な表現（35文字以内）。")
    summary_1: str = Field(description="3行結論の1つ目。事実のみで構成され、推測や感想を含めない。体言止めで30文字以内。")
    summary_2: str = Field(description="3行結論の2つ目。事実のみで構成され、推測や感想を含めない。体言止めで30文字以内。")
    summary_3: str = Field(description="3行結論の3つ目。事実のみで構成され、推測や感想を含めない。体言止めで30文字以内。")
    summary_detail: str = Field(
        description="""
        500〜700文字程度。
        初心者にもわかりやすいように、元記事（英語）に登場する具体的なデータ（数値、固有名詞、または重要な一節の日本語訳）を、
        必ず適切に「引用」しながら、技術的な背景、GoogleやOpenAIなどの企業の狙い、なぜこれが重要なのか、
        何が変わり、業界や一般ユーザーに今後どのような影響があるか、今後の可能性を含めて詳細に記述してください。
        内容が浅くなったり、文字数が少なくならないよう厳重に詳しく書いてください。
        """
    )
    explanation_intro: str = Field(description="初心者向け解説の導入。興味を惹く一文。50文字以内。")
    # 🔴 改善①（解説長文化）：300〜500文字での詳細な「たとえ話」をスキーマで義務付け
    explanation_full: str = Field(description="初心者向け解説の続き。「たとえば〜」から始まる具体的な比喩を必ず含め、専門用語を使わずに中学生でも理解できるように優しく噛み砕いた詳細な解説。300〜500文字程度で、文章が短くならないよう具体例を多く記述してください。")
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

    # 🔴 改善①（解説長文化）：プロンプトでの指示文を300〜500文字に超強化！
    prompt = f"""
    あなたは、「AI初心者でも直感的に理解できる」日本語コンテンツを作成する、日本最高レベルのAIニュース編集者です。
    以下の【厳守ルール】に厳密に従い、海外AI記事の日本語コンテンツを生成してください。

    【厳守ルール】
    - 専門用語を限界まで噛み砕き、中学生でもイメージできる平易な日本語にしてください。
    - 誇張を排し、客観的かつ断定しすぎない知的なトーンを保ってください。
    - summary_detailは浅い説明で終わらせず、元記事（英語）の具体的なデータ、数値、固有名詞、または重要な一節（日本語訳）を適切に「引用」しながら、「なぜ重要なのか」「従来との違い」「今後何が変わるのか」まで踏み込んで、必ず500〜700文字程度のボリュームで極めて詳細に論理的に説明してください。
    - explanation_fullは短い説明で終わらせず、「たとえば〜」から始まる具体的な例え話を深く掘り下げ、300〜500文字程度の十分なボリュームで、中学生でも情景がイメージできるように極めて優しく丁寧に解説してください。
    - titleは日本のエンジニアやビジネスマンがクリックしたくなる自然な表現にしてください。
    - slugはアルファベット小文字とハイフンのみ（例: gemini-2-flash-release）で指定してください。

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

    # 🔴 改善③（1ファイル管理）：個別記事の生成・再ビルドを一元管理するためのヘルパー関数「build_page」を呼び出すように設計
    build_page(
        body_template_path="template_article.html",
        title=article_dict["title"],
        date_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        date_ja=datetime.now().strftime("%Y年%m月%d日 %H:%M"),
        source_url=source_url,
        source_name=source_name,
        replacements={
            "{{SUMMARY_1}}": article_dict["summary_1"],
            "{{SUMMARY_2}}": article_dict["summary_2"],
            "{{SUMMARY_3}}": article_dict["summary_3"],
            "{{SUMMARY_DETAIL}}": article_dict["summary_detail"],
            "{{EXPLANATION_INTRO}}": article_dict["explanation_intro"],
            "{{EXPLANATION_FULL}}": article_dict["explanation_full"],
            "{{ACTION_1}}": article_dict["action_1"],
            "{{ACTION_2}}": article_dict["action_2"]
        },
        output_path=os.path.join("articles", f"{slug}.html"),
        is_article=True,
        slug=slug
    )

    # 中間データの保存
    output_json_path = os.path.join("data", f"{slug}.json")
    article_dict["source_url"] = source_url
    article_dict["source_name"] = source_name
    article_dict["template_version"] = TEMPLATE_VERSION

    try:
        tmp_json_path = output_json_path + ".tmp"
        with open(tmp_json_path, "w", encoding="utf-8") as f:
            json.dump(article_dict, f, ensure_ascii=False, indent=2)
        os.replace(tmp_json_path, output_json_path)
        logging.info(f"記事生成成功: {slug}")
        return slug
    except Exception as e:
        logging.error(f"JSON保存失敗: {e}")
        return ""

# ==========================================
# 🔴 新機能：【一元管理の心臓】全ページ共通レイアウト結合（ビルド）ヘルパー関数
# ==========================================
def build_page(body_template_path, title, date_iso, date_ja, source_url, source_name, replacements, output_path, is_article=False, slug=""):
    """layout.html と各ページの中身（テンプレート）を合体させ、変数を安全に置換して書き出す"""
    try:
        # 1. 唯一のデザインファイルである layout.html の読み込み
        if not os.path.exists("layout.html"):
            logging.error("layout.html が見つかりません。")
            return

        with open("layout.html", "r", encoding="utf-8") as f:
            layout_content = f.read()

        # 2. 中身（ボディ）テンプレートの読み込み
        if not os.path.exists(body_template_path):
            logging.error(f"テンプレート '{body_template_path}' が見つかりません。")
            return

        with open(body_template_path, "r", encoding="utf-8") as f:
            body_content = f.read()

        # 3. 合体！
        combined_content = layout_content.replace("{{BODY_CONTENT}}", body_content)

        # 4. パスの自動調整（個別記事は /style.css、トップページは style.css など自動的に最適化）
        if is_article:
            combined_content = combined_content.replace("{{CSS_PATH}}", "/style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "/script.js")
            combined_content = combined_content.replace("{{EXPLANATION_INTRO}}", replacements.get("{{EXPLANATION_INTRO}}", ""))
            
            # 個別記事用の構造化データを自動埋め込み
            structured_data = f"""
            <script type="application/ld+json">
            {{
              "@context": "https://schema.org",
              "@type": "NewsArticle",
              "headline": "{title}",
              "datePublished": "{date_iso}",
              "author": {{"@type": "Person", "name": "ちゃろ"}}
            }}
            </script>
            """
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)
        else:
            combined_content = combined_content.replace("{{CSS_PATH}}", "style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "script.js")
            combined_content = combined_content.replace("{{EXPLANATION_INTRO}}", replacements.get("{{EXPLANATION_INTRO}}", "最新のAIニュースをお届けします。"))
            
            # トップ・アーカイブ用の構造化データを自動埋め込み
            structured_data = """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "WebSite",
              "name": "AI Frontier Lab",
              "url": "https://ai-example.pray-power-is-god-and-cocoro.com/"
            }
            </script>
            """
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)

        # 5. 変数の置換
        combined_content = combined_content.replace("{{TITLE}}", title)
        combined_content = combined_content.replace("{{DATE_ISO}}", date_iso)
        combined_content = combined_content.replace("{{DATE_JA}}", date_ja)
        combined_content = combined_content.replace("{{SOURCE_URL}}", html.escape(source_url))
        combined_content = combined_content.replace("{{SOURCE_NAME}}", html.escape(source_name))

        # 個別置換の実行
        for placeholder, value in replacements.items():
            combined_content = combined_content.replace(placeholder, value)

        # 原子性を維持した書き出し（上書き）
        tmp_output_path = output_path + ".tmp"
        with open(tmp_output_path, "w", encoding="utf-8") as f:
            f.write(combined_content)
        os.replace(tmp_output_path, output_path)

    except Exception as e:
        logging.error(f"build_page 実行中にエラーが発生しました ({output_path}): {e}")

# ==========================================
# 7. template_index.html と template_article.html を基にした全自動一括再ビルド（SSGコンパイル）
# ==========================================
def rebuild_index_and_rotate_storage():
    """蓄積されたJSONから最新データを反映したサイト全体を全自動で再ビルドする"""
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

        # 最新順にソート
        all_articles.sort(key=lambda x: x[0], reverse=True)

        # 容量パンク防止のローテーション物理削除
        if len(all_articles) > MAX_ARTICLES_LIMIT:
            logging.info(f"記事数が上限を超えたため、古いファイルを自動削除します。")
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

        if not all_articles:
            logging.info("データフォルダが空のため、一覧の更新を保留します。")
            return

        # 1. すべての個別記事HTMLを最新のテンプレートで一括再ビルド（デグレ完全防止）
        for mtime, art in all_articles:
            a_slug = sanitize_slug(art["slug"])
            a_date_ja = datetime.fromtimestamp(mtime).strftime("%Y年%m月%d日 %H:%M")
            a_date_iso = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S+09:00")
            
            build_page(
                body_template_path="template_article.html",
                title=art["title"],
                date_iso=a_date_iso,
                date_ja=a_date_ja,
                source_url=art.get("source_url", "#"),
                source_name=art.get("source_name", "ソース"),
                replacements={
                    "{{SUMMARY_1}}": art["summary_1"],
                    "{{SUMMARY_2}}": art["summary_2"],
                    "{{SUMMARY_3}}": art["summary_3"],
                    "{{SUMMARY_DETAIL}}": art["summary_detail"],
                    "{{EXPLANATION_INTRO}}": art["explanation_intro"],
                    "{{EXPLANATION_FULL}}": art["explanation_full"],
                    "{{ACTION_1}}": art["action_1"],
                    "{{ACTION_2}}": art["action_2"]
                },
                output_path=os.path.join("articles", f"{a_slug}.html"),
                is_article=True,
                slug=a_slug
            )

        # 2. ヒーロー記事（最新の1位）のデータを取得
        _, hero_art = all_articles[0]
        hero_date_ja = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        hero_date_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

        # 3. 2番目以降の古い記事（2位〜最大30位）をグリッド用のカードに変換
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

        # 4. index.html をビルド（layout.html と template_index.html をガッチャンコ！）
        build_page(
            body_template_path="template_index.html",
            title=hero_art["title"],
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url=hero_art.get("source_url", "#"),
            source_name=hero_art.get("source_name", "ソース"),
            replacements={
                "{{SUMMARY_1}}": hero_art["summary_1"],
                "{{SUMMARY_2}}": hero_art["summary_2"],
                "{{SUMMARY_3}}": hero_art["summary_3"],
                "{{SUMMARY_DETAIL}}": hero_art["summary_detail"],
                "{{EXPLANATION_INTRO}}": hero_art["explanation_intro"],
                "{{EXPLANATION_FULL}}": hero_art["explanation_full"],
                "{{ACTION_1}}": hero_art["action_1"],
                "{{ACTION_2}}": hero_art["action_2"],
                "{{ARTICLES_GRID}}": articles_html
            },
            output_path="index.html",
            is_article=False
        )

        # 5. index.html のビルド後に、過去の全件アーカイブページ「archive.html」を自動生成（デザイン完全一致）
        archive_articles_html = ""
        for _, art in all_articles:
            a_title = html.escape(art["title"])
            a_intro = html.escape(art["explanation_intro"])
            a_slug = sanitize_slug(art["slug"])
            archive_articles_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span>AI News</span>
                        <span>Archived</span>
                    </div>
                    <h3>{a_title}</h3>
                    <p>{a_intro}</p>
                    <a href="articles/{a_slug}.html">続きを読む &rarr;</a>
                </article>
            """

        # index.html をベースにして、安全に archive.html のヒーローエリアをヘッダーへ置換
        if os.path.exists("index.html"):
            with open("index.html", "r", encoding="utf-8") as f:
                index_content = f.read()

            archive_hero_header_html = """
            <div class="archive-header" style="text-align: center; padding: 40px 0; margin-bottom: 40px;">
                <span class="section-mini" style="background: var(--tag-bg); padding: 6px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 600;">ARCHIVE</span>
                <h2 style="font-size: 2.2rem; font-weight: 800; margin: 20px 0; letter-spacing: -0.02em;">過去の記事一覧</h2>
                <p style="color: var(--text-muted); max-width: 500px; margin: 0 auto; line-height: 1.6;">これまでに AI Frontier Lab が配信した、日本一わかりやすい要約記事のアーカイブです。</p>
            </div>
            """

            archive_content = index_content
            hero_pattern = re.compile(r"<article class=\"post fade-element\">.*?</article>", re.DOTALL)
            archive_content = hero_pattern.sub(archive_hero_header_html, archive_content)
            archive_content = archive_content.replace(articles_html, archive_articles_html)
            archive_content = archive_content.replace(f"<title>{html.escape(hero_art['title'])} | AI Frontier Lab</title>", "<title>過去の記事一覧 | AI Frontier Lab</title>")

            tmp_archive_path = "archive.html.tmp"
            with open(tmp_archive_path, "w", encoding="utf-8") as f:
                f.write(archive_content)
            os.replace(tmp_archive_path, "archive.html")

            print("✅ index.html, archive.html, およびすべての個別記事HTMLの再ビルドが完了しました！")

    except Exception as e:
        logging.error(f"再ビルド中にエラーが発生しました: {e}")

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
    
    # 挙動テスト用の模擬記事自動生成
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

    # 新着記事の有無に関わらず、起動されたら必ず最新データを基に「再ビルド」を実行！
    rebuild_index_and_rotate_storage()

if __name__ == "__main__":
    main()

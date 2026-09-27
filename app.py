import os
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash
from flask_cors import CORS
from flask_login import LoginManager, login_required, current_user
from openai import OpenAI, RateLimitError, AuthenticationError
from pypinyin import lazy_pinyin, Style
from docx import Document
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
import html
import platform
from dotenv import load_dotenv
from models import db, User, QueryLog, Subscription, PLANS, get_app_price, app_is_free
from auth import auth_bp
from payments import payments_bp
from admin import admin_bp

# Load environment variables (override any stale shell values)
load_dotenv(override=True)

app = Flask(__name__)
CORS(app)

# Configuration
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///composition.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Register blueprints
app.register_blueprint(auth_bp, url_prefix='/auth')
app.register_blueprint(payments_bp, url_prefix='/payment')
app.register_blueprint(admin_bp, url_prefix='/admin')

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Markers that identify an upstream billing/quota failure rather than genuine
# rate limiting. OpenAI populates these inconsistently across error shapes
# (sometimes `code`, sometimes only `type`, sometimes only the message), so all
# three fields are checked.
QUOTA_MARKERS = (
    'insufficient_quota',
    'credit_balance_exhausted',
    'billing_hard_limit_reached',
    'billing_not_active',
    'no credits remaining',
    'exceeded your current quota',
    'check your plan and billing',
)


def openai_error_fields(exc):
    """Pull (code, type, message) out of an OpenAI API error, whatever its shape."""
    body = getattr(exc, 'body', None)
    err = body.get('error') if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {}
    return (
        str(err.get('code') or ''),
        str(err.get('type') or ''),
        str(err.get('message') or '') or str(exc),
    )


def is_quota_error(exc):
    """True when a 429 is really 'the OpenAI account is out of credits'."""
    code, err_type, message = openai_error_fields(exc)
    haystack = f'{code} {err_type} {message}'.lower()
    return any(marker in haystack for marker in QUOTA_MARKERS)


# Register Chinese fonts for PDF generation
def register_chinese_fonts():
    """Register Chinese fonts for ReportLab PDF generation"""
    system = platform.system()
    chinese_font_paths = []
    
    if system == 'Darwin':  # macOS
        # Try common macOS Chinese fonts - check multiple locations
        potential_paths = [
            '/System/Library/Fonts/Supplemental/PingFang.ttc',
            '/System/Library/Fonts/PingFang.ttc',
            '/System/Library/Fonts/STHeiti Light.ttc',
            '/System/Library/Fonts/Supplemental/Songti.ttc',
            '/Library/Fonts/Microsoft/SimHei.ttf',
            '/System/Library/Fonts/STSong.ttc',
            '/System/Library/Fonts/STKaiti.ttc',
        ]
        # Also check user fonts
        home = os.path.expanduser('~')
        potential_paths.extend([
            f'{home}/Library/Fonts/SimHei.ttf',
            f'{home}/Library/Fonts/SimSun.ttf',
        ])
        chinese_font_paths = potential_paths
    elif system == 'Linux':
        chinese_font_paths = [
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            '/usr/share/fonts/truetype/arphic/uming.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf',
        ]
    elif system == 'Windows':
        chinese_font_paths = [
            'C:/Windows/Fonts/simsun.ttc',
            'C:/Windows/Fonts/simhei.ttf',
            'C:/Windows/Fonts/msyh.ttc',
            'C:/Windows/Fonts/simkai.ttf',
        ]
    
    # Try to register a Chinese font
    chinese_font_registered = False
    registered_font_name = None
    
    for font_path in chinese_font_paths:
        if os.path.exists(font_path):
            try:
                # For TTC files, try different approaches
                if font_path.endswith('.ttc'):
                    # Try to register with subfontIndex 0 first
                    try:
                        pdfmetrics.registerFont(TTFont('ChineseFont', font_path, subfontIndex=0))
                        chinese_font_registered = True
                        registered_font_name = 'ChineseFont'
                        break
                    except:
                        # If that fails, try without subfontIndex
                        try:
                            pdfmetrics.registerFont(TTFont('ChineseFont', font_path))
                            chinese_font_registered = True
                            registered_font_name = 'ChineseFont'
                            break
                        except:
                            continue
                else:
                    pdfmetrics.registerFont(TTFont('ChineseFont', font_path))
                    chinese_font_registered = True
                    registered_font_name = 'ChineseFont'
                    break
            except Exception as e:
                # Continue to next font
                continue
    
    return chinese_font_registered, registered_font_name

# Register fonts on startup
CHINESE_FONT_AVAILABLE, CHINESE_FONT_NAME = register_chinese_fonts()

# Grade-specific Chinese character lists
# Characters students should know by each grade level
# Characters not in the list for a given grade will get Pinyin

GRADE_1_CHARS = set('一二三四五六七八九十百千万个年月日时天地上大小多少前后左右中上下你我他她它是的在不了有和人这大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞')

# Grade 2 adds slightly more advanced characters
GRADE_2_CHARS = GRADE_1_CHARS | set('环境科学艺术文化教育社会历史自然地理政治经济法律医学技术工程商业金融农业工业交通通讯能源资源保护发展进步创新改革开放建设服务管理')

# Grade 3-4: Intermediate level
GRADE_3_CHARS = GRADE_2_CHARS | set('古代现代传统现代理论实践知识技能能力素质修养品格品德道德品质习惯态度价值观人生观世界观方法论哲学思维思想观点意见建议方案计划目标目的意义价值作用影响效果结果成果成就贡献责任义务权利机会挑战困难问题矛盾冲突解决方案策略措施办法手段途径方式方法技巧经验教训')

# Grade 5-6: Upper elementary
GRADE_5_CHARS = GRADE_3_CHARS | set('社会制度政治体制文化传统历史发展民族国家国际关系合作交流竞争对抗协商谈判协议合同条约法律条文规章制度政策法规措施办法机制体制模式形式内容实质本质特征特点性质类型种类范畴领域范围层次水平程度')

# Grade 7-9: Middle school
GRADE_7_CHARS = GRADE_5_CHARS | set('文明文化传统现代理论学术研究分析探讨论述阐述解释说明描述表达叙述记述记录记载传播传承发扬光大继承发展创新改革改善提高提升增强完善优化调整修正改进改良变革转变转化演变进化')

# Grade 10-12: High school
GRADE_10_CHARS = GRADE_7_CHARS | set('概念定义原理规律法则定理公理公式方程式函数关系联系相互影响作用机制原理理论基础实践应用具体实例案例事例现象状态过程阶段步骤环节要素因素条件前提基础背景环境氛围气氛')

# University/Adult: Very advanced, only rare characters need Pinyin
GRADE_UNIVERSITY_CHARS = GRADE_10_CHARS | set('阐述诠释论证辨析阐发阐扬阐绎诠注诠解诠析诠评诠论诠议诠证诠度诠授诠译诠审诠试诠校诠检诠查诠验诠测诠量诠衡诠度诠准诠正诠确诠定诠明诠清诠楚诠晰诠白诠亮诠暗诠黑诠灰诠色诠彩诠调诠味诠感诠觉诠知诠识诠见诠视诠观诠察诠看诠望诠眺诠瞰诠瞻诠瞩诠目诠睛诠眼诠瞳诠眸诠珠诠球诠圆诠方诠正诠直诠曲诠弯诠扭诠折诠转诠旋诠绕诠围诠环诠圈诠环')

# Grade level to character set mapping
GRADE_CHARS = {
    1: GRADE_1_CHARS,
    2: GRADE_2_CHARS,
    3: GRADE_3_CHARS,
    4: GRADE_3_CHARS,
    5: GRADE_5_CHARS,
    6: GRADE_5_CHARS,
    7: GRADE_7_CHARS,
    8: GRADE_7_CHARS,
    9: GRADE_7_CHARS,
    10: GRADE_10_CHARS,
    11: GRADE_10_CHARS,
    12: GRADE_10_CHARS,
    'university': GRADE_UNIVERSITY_CHARS,
    'adult': GRADE_UNIVERSITY_CHARS,
}

# Default to grade 1 for backward compatibility
ELEMENTARY_CHINESE_CHARS = GRADE_1_CHARS

def is_hard_chinese_char(char, grade=1):
    """Check if a Chinese character is hard for the given grade level"""
    # Get the character set for this grade
    grade_set = GRADE_CHARS.get(grade, GRADE_1_CHARS)
    return '\u4e00' <= char <= '\u9fff' and char not in grade_set

def add_pinyin_to_chinese(text, grade=1):
    """Add Pinyin above every Chinese character using ruby annotations."""
    result = []
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            pinyin = lazy_pinyin(char, style=Style.TONE)[0]
            result.append(f'<ruby>{char}<rt>{pinyin}</rt></ruby>')
        elif char == '\n':
            result.append('<br>')
        elif char == '<':
            result.append('&lt;')
        elif char == '>':
            result.append('&gt;')
        elif char == '&':
            result.append('&amp;')
        else:
            result.append(char)
    return ''.join(result)

@app.route('/')
def index():
    """Main page - requires authentication"""
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login'))
    
    # Force fresh query to get latest plan info (important after plan changes)
    from sqlalchemy.orm import object_session
    from flask import make_response
    
    session = object_session(current_user)
    if session:
        session.expire_all()
    
    # Get fresh user from database
    fresh_user = db.session.query(User).filter_by(id=current_user.id).first()
    
    # Get user's plan info from fresh user object
    plan_type = fresh_user.get_plan()
    plan = PLANS.get(plan_type, PLANS['free'])
    remaining_queries = fresh_user.get_remaining_queries()
    
    # Create response with cache headers
    response = make_response(render_template('index.html', 
                         user=fresh_user,
                         plan=plan,
                         plan_type=plan_type,
                         remaining_queries=remaining_queries,
                         app_price=get_app_price(),
                         app_is_free=app_is_free()))
    
    # Add cache-control headers for the main page too
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

@app.route('/generate', methods=['POST'])
@login_required
def generate_article():
    """Generate article - requires authentication and checks query limits"""
    # Check if user can make a query
    if not current_user.can_make_query():
        plan_type = current_user.get_plan()
        plan = PLANS.get(plan_type, PLANS['free'])
        if plan.get('queries_per_day'):
            limit_msg = f"{plan['queries_per_day']} queries per day"
        elif plan.get('queries_per_week'):
            limit_msg = f"{plan['queries_per_week']} queries per week"
        elif plan.get('queries_per_month'):
            limit_msg = f"{plan['queries_per_month']} queries per month"
        else:
            limit_msg = "query limit"
        return jsonify({
            'error': f'Query limit reached. Your plan allows {limit_msg}. Please upgrade or wait for the next period.',
            'success': False
        }), 403
    
    try:
        data = request.json
        topic = data.get('topic', '')
        language = data.get('language', 'English')
        total_words = int(data.get('total_words', 500))
        grade = data.get('grade', 1)
        language_level = data.get('language_level', 'intermediate')
        
        # Validate and normalize grade
        if isinstance(grade, str):
            if grade.lower() in ['university', 'adult']:
                grade = grade.lower()
            else:
                try:
                    grade = int(grade)
                except:
                    grade = 1
        elif isinstance(grade, int):
            if grade < 1 or grade > 12:
                grade = 1
        else:
            grade = 1
        
        # Validate language level
        valid_levels = ['beginner', 'intermediate', 'advanced', 'professional']
        if language_level not in valid_levels:
            language_level = 'intermediate'
        
        if not topic:
            return jsonify({'error': 'Topic is required'}), 400
        
        # Create prompt for OpenAI
        language_map = {
            'English': 'English',
            'Chinese': 'Chinese (Simplified)',
            'Spanish': 'Spanish',
            'French': 'French',
            'German': 'German',
            'Japanese': 'Japanese',
            'Korean': 'Korean'
        }
        
        target_language = language_map.get(language, 'English')
        
        # Language level instructions
        level_instructions = {
            'beginner': 'Use simple vocabulary, short sentences, and basic grammar structures. Avoid complex terms and jargon. Write as if for someone learning the language.',
            'intermediate': 'Use moderate vocabulary and varied sentence structures. Include some intermediate-level terms but avoid highly specialized jargon. Write for general readers.',
            'advanced': 'Use sophisticated vocabulary, complex sentence structures, and advanced grammar. Include specialized terms where appropriate. Write for educated readers.',
            'professional': 'Use expert-level vocabulary, highly sophisticated sentence structures, and professional terminology. Include domain-specific jargon as needed. Write for professionals and experts in the field.'
        }
        
        level_instruction = level_instructions.get(language_level, level_instructions['intermediate'])
        
        prompt = f"""Write a comprehensive article about "{topic}" in {target_language}.
        
Requirements:
- The article must be exactly {total_words} words (or characters if writing in Chinese/Japanese/Korean)
- Language Level: {language_level.title()} - {level_instruction}
- Structure the article with an introduction, body paragraphs, and conclusion
- Make it informative and well-organized
- Ensure the vocabulary and sentence complexity match the {language_level} level

Article:"""
        
        # System message based on language level
        system_messages = {
            'beginner': f"You are a language learning assistant. Write articles in {target_language} using simple, clear language appropriate for beginners. Use basic vocabulary and straightforward sentence structures.",
            'intermediate': f"You are a professional article writer. Write articles in {target_language} using moderate vocabulary and varied sentence structures appropriate for intermediate-level readers.",
            'advanced': f"You are an expert article writer. Write sophisticated articles in {target_language} using advanced vocabulary and complex sentence structures for educated readers.",
            'professional': f"You are a subject matter expert and professional writer. Write expert-level articles in {target_language} using professional terminology and sophisticated language for specialists and professionals."
        }
        
        system_message = system_messages.get(language_level, system_messages['intermediate'])
        
        # Call OpenAI API
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        
        article = response.choices[0].message.content.strip()
        
        # Add Pinyin to Chinese text if language is Chinese
        if language == 'Chinese':
            article = add_pinyin_to_chinese(article, grade=grade)
        
        # Log the query
        query_log = QueryLog(
            user_id=current_user.id,
            topic=topic,
            language=language
        )
        db.session.add(query_log)
        db.session.commit()
        
        # Get remaining queries for response
        remaining = current_user.get_remaining_queries()
        
        return jsonify({
            'article': article,
            'success': True,
            'remaining_queries': remaining
        })

    except RateLimitError as e:
        # Upstream OpenAI account problem - NOT the user's plan or query allowance.
        code, err_type, upstream_message = openai_error_fields(e)
        # Always log the raw upstream error; the user-facing text is deliberately vague.
        print(f"OpenAI 429 for user {current_user.username}: "
              f"code={code!r} type={err_type!r} message={upstream_message!r}")

        if is_quota_error(e):
            message = ('The article service is temporarily unavailable: the site\'s OpenAI '
                       'API account is out of credits. This is not a limit on your account - '
                       'please contact the site administrator.')
        else:
            message = ('The article service is busy right now (rate limited upstream). '
                       'Please try again in a moment.')

        payload = {'error': message, 'success': False}
        # Admins get the raw provider detail so they can diagnose without log access.
        if getattr(current_user, 'is_admin', False):
            payload['upstream_error'] = upstream_message
        return jsonify(payload), 503

    except AuthenticationError as e:
        code, err_type, upstream_message = openai_error_fields(e)
        print(f"OpenAI auth failure for user {current_user.username}: "
              f"code={code!r} type={err_type!r} message={upstream_message!r}")
        payload = {
            'error': ('The article service is misconfigured: the OpenAI API key is missing or '
                      'invalid. This is not a limit on your account - please contact the site '
                      'administrator.'),
            'success': False
        }
        if getattr(current_user, 'is_admin', False):
            payload['upstream_error'] = upstream_message
        return jsonify(payload), 503

    except Exception as e:
        return jsonify({'error': str(e), 'success': False}), 500

def html_to_text_with_pinyin_above(html_content):
    """Convert HTML with <ruby>/<rt> (or legacy spans) to plain text with Pinyin on line above."""
    import re

    text = html_content.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')

    # --- Handle <ruby> format ---
    pinyin_pairs = []

    def extract_ruby(match):
        char = match.group(1).strip()
        pinyin = match.group(2).strip()
        idx = len(pinyin_pairs)
        pinyin_pairs.append((pinyin, char))
        return f'__PINYIN_{idx}__'

    text = re.sub(r'<ruby>([^<]+)<rt>([^<]+)</rt></ruby>', extract_ruby, text)

    # --- Handle legacy span format ---
    def extract_span(match):
        full_match = match.group(0)
        pinyin_match = re.search(r'<span class="pinyin-line">([^<]+)</span>', full_match)
        char_match = re.search(r'<span class="char-inline">([^<]+)</span>', full_match)
        if not pinyin_match or not char_match:
            pinyin_match = re.search(r'<span class="pinyin">([^<]+)</span>', full_match)
            char_match = re.search(r'<span class="char">([^<]+)</span>', full_match)
        if pinyin_match and char_match:
            idx = len(pinyin_pairs)
            pinyin_pairs.append((pinyin_match.group(1), char_match.group(1)))
            return f'__PINYIN_{idx}__'
        return match.group(0)

    text = re.sub(r'<span class="pinyin-wrapper">.*?</span>', extract_span, text, flags=re.DOTALL)
    text = re.sub(r'<span class="pinyin-char">.*?</span>', extract_span, text, flags=re.DOTALL)

    # Remove remaining HTML tags and unescape entities
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)

    # Build two-line output (pinyin line above char line) for each text line
    result_lines = []
    for line in text.split('\n'):
        if '__PINYIN_' not in line:
            result_lines.append(line)
            continue

        pinyin_parts = []
        char_parts = []
        i = 0
        while i < len(line):
            marker_match = re.search(r'__PINYIN_(\d+)__', line[i:])
            if marker_match:
                before = line[i:i + marker_match.start()]
                for ch in before:
                    char_parts.append(ch)
                    pinyin_parts.append('  ' if '\u4e00' <= ch <= '\u9fff' else ' ')
                idx = int(marker_match.group(1))
                if idx < len(pinyin_pairs):
                    py, ch = pinyin_pairs[idx]
                    width = max(len(py), 2)
                    pinyin_parts.append(py.ljust(width))
                    char_parts.append(ch.ljust(width))
                i += marker_match.end()
            else:
                for ch in line[i:]:
                    char_parts.append(ch)
                    pinyin_parts.append('  ' if '\u4e00' <= ch <= '\u9fff' else ' ')
                break

        pinyin_line = ''.join(pinyin_parts).rstrip()
        char_line = ''.join(char_parts).rstrip()
        if pinyin_line.strip():
            result_lines.append(pinyin_line)
        result_lines.append(char_line)

    return '\n'.join(result_lines)

def html_to_text(html_content):
    """Convert HTML to plain text, rendering Pinyin as char(pinyin)."""
    import re

    # Handle <ruby> format
    text = re.sub(r'<ruby>([^<]+)<rt>([^<]+)</rt></ruby>',
                  lambda m: f"{m.group(1)}({m.group(2)})", html_content)

    # Handle legacy span format
    def replace_span(match):
        full_match = match.group(0)
        pinyin_match = re.search(r'<span class="pinyin-line">([^<]+)</span>', full_match)
        char_match = re.search(r'<span class="char-inline">([^<]+)</span>', full_match)
        if pinyin_match and char_match:
            return f"{char_match.group(1)}({pinyin_match.group(1)})"
        return match.group(0)

    text = re.sub(r'<span class="pinyin-wrapper">.*?</span>', replace_span, text, flags=re.DOTALL)
    text = re.sub(r'<span class="pinyin-char">.*?</span>', replace_span, text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    return text

@app.route('/download', methods=['POST'])
@login_required
def download_article():
    """Download article - requires authentication"""
    # All authenticated users can download (free plan included)
    
    try:
        data = request.json
        article = data.get('article', '')
        format_type = data.get('format', 'txt')
        filename = data.get('filename', 'article')
        
        if not article:
            return jsonify({'error': 'Article content is required'}), 400
        
        # Check if article contains HTML (Pinyin format)
        is_html = '<ruby>' in article or '<span class="pinyin-wrapper">' in article or '<span class="pinyin-char">' in article
        
        if format_type == 'txt':
            # Convert HTML to text with Pinyin above if needed
            if is_html:
                article_text = html_to_text_with_pinyin_above(article)
            else:
                article_text = article
            output = BytesIO()
            output.write(article_text.encode('utf-8'))
            output.seek(0)
            return send_file(
                output,
                mimetype='text/plain',
                as_attachment=True,
                download_name=f'{filename}.txt'
            )
        
        elif format_type == 'doc':
            doc = Document()
            # Convert HTML to text with Pinyin above for DOC format
            if is_html:
                article_text = html_to_text_with_pinyin_above(article)
            else:
                article_text = article
            # Process line by line - each line becomes a paragraph
            # This preserves Pinyin lines above character lines
            lines = article_text.split('\n')
            for line in lines:
                if line.strip():
                    para = doc.add_paragraph(line.strip())
                    # Make Pinyin lines smaller if they don't contain Chinese
                    if not any('\u4e00' <= c <= '\u9fff' for c in line.strip()):
                        # Likely a Pinyin line
                        for run in para.runs:
                            run.font.size = None  # Use default smaller size
                            if hasattr(run.font, 'size'):
                                run.font.size = None
            
            output = BytesIO()
            doc.save(output)
            output.seek(0)
            return send_file(
                output,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                as_attachment=True,
                download_name=f'{filename}.docx'
            )
        
        elif format_type == 'pdf':
            output = BytesIO()
            doc = SimpleDocTemplate(output, pagesize=letter)
            styles = getSampleStyleSheet()
            
            # Create a style with Chinese font support
            if CHINESE_FONT_AVAILABLE and CHINESE_FONT_NAME:
                chinese_style = ParagraphStyle(
                    'ChineseStyle',
                    parent=styles['Normal'],
                    fontName=CHINESE_FONT_NAME,
                    fontSize=12,
                    leading=18,
                )
                pinyin_style = ParagraphStyle(
                    'PinyinStyle',
                    parent=styles['Normal'],
                    fontName='Helvetica',  # Pinyin uses Latin characters
                    fontSize=10,
                    leading=14,
                )
            else:
                chinese_style = styles['Normal']
                pinyin_style = styles['Normal']
            
            story = []
            
            # Convert HTML to text with Pinyin above for PDF format
            if is_html:
                article_text = html_to_text_with_pinyin_above(article)
            else:
                article_text = article
            
            # Check if text contains Chinese characters
            has_chinese = any('\u4e00' <= char <= '\u9fff' for char in article_text)
            
            # Process line by line to preserve Pinyin above characters
            lines = article_text.split('\n')
            for line in lines:
                if line.strip():
                    # Escape HTML for PDF
                    line_escaped = html.escape(line.strip())
                    # Check if this line is likely Pinyin (no Chinese characters)
                    is_pinyin_line = has_chinese and not any('\u4e00' <= c <= '\u9fff' for c in line.strip()) and any(c.isalpha() or c.isdigit() for c in line.strip())
                    # Use appropriate style
                    if is_pinyin_line and CHINESE_FONT_AVAILABLE:
                        style_to_use = pinyin_style
                    elif has_chinese and CHINESE_FONT_AVAILABLE:
                        style_to_use = chinese_style
                    else:
                        style_to_use = styles['Normal']
                    
                    p = Paragraph(line_escaped, style_to_use)
                    story.append(p)
                    # Add smaller spacing for Pinyin lines
                    if is_pinyin_line:
                        story.append(Spacer(1, 2))
                    else:
                        story.append(Spacer(1, 6))
            
            # Add paragraph spacing at the end
            story.append(Spacer(1, 12))
            
            doc.build(story)
            output.seek(0)
            return send_file(
                output,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=f'{filename}.pdf'
            )
        
        elif format_type == 'html':
            # Create a complete HTML document with styling
            html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{filename}</title>
    <style>
        body {{
            font-family: 'Georgia', 'Times New Roman', serif;
            max-width: 800px;
            margin: 40px auto;
            padding: 20px;
            line-height: 2.2;
            font-size: 1em;
        }}
        ruby {{
            ruby-align: center;
        }}
        rt {{
            font-size: 0.55em;
            color: #555;
            font-family: 'Arial', sans-serif;
            letter-spacing: 0;
        }}
    </style>
</head>
<body>
{article}
</body>
</html>"""
            
            output = BytesIO()
            output.write(html_content.encode('utf-8'))
            output.seek(0)
            return send_file(
                output,
                mimetype='text/html',
                as_attachment=True,
                download_name=f'{filename}.html'
            )
        
        else:
            return jsonify({'error': 'Invalid format'}), 400
    
    except Exception as e:
        return jsonify({'error': str(e), 'success': False}), 500

# Create database tables and seed required accounts
def seed_accounts():
    """Create built-in accounts if they don't exist."""
    from werkzeug.security import generate_password_hash
    from datetime import datetime, timedelta

    accounts = [
        {
            'username': 'Eric',
            'email': 'eric@composition.app',
            'password': os.getenv('ERIC_PASSWORD', ''),
            'plan': 'unlimited',
        }
    ]

    if not accounts[0]['password']:
        print("WARNING: ERIC_PASSWORD env var not set, skipping Eric account seed.")
        return

    for acc in accounts:
        user = User.query.filter_by(username=acc['username']).first()
        if not user:
            user = User(
                username=acc['username'],
                email=acc['email'],
                password_hash=generate_password_hash(acc['password']),
                is_active=True,
            )
            db.session.add(user)
            db.session.flush()
            print(f"Seeded user: {acc['username']}")

        # Ensure unlimited subscription exists and is active
        sub = Subscription.query.filter_by(user_id=user.id, plan_type=acc['plan']).first()
        if not sub:
            sub = Subscription(
                user_id=user.id,
                plan_type=acc['plan'],
                status='active',
                amount=0,
                current_period_start=datetime.utcnow(),
                current_period_end=datetime.utcnow() + timedelta(days=36500),  # 100 years
            )
            db.session.add(sub)
            print(f"Seeded {acc['plan']} subscription for: {acc['username']}")

    db.session.commit()

with app.app_context():
    db.create_all()
    seed_accounts()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)


import os
import discord
from discord.ext import commands
import asyncio
import time as time_module
from google import genai
from google.genai import types
from dotenv import load_dotenv
import re
from typing import Dict, List
from flask import Flask
from threading import Thread

# โหลด Environment Variables
load_dotenv()

# --- 1. ตั้งค่าพื้นฐาน (Config) ---
QUERY_CHANNEL_ID = int(os.environ.get('QUERY_CHANNEL_ID', 0))

# อ่านค่า Admin IDs
raw_owner_ids = os.environ.get('BOT_OWNER_IDS', '')
BOT_OWNER_IDS = [int(x.strip()) for x in raw_owner_ids.split(',') if x.strip().isdigit()]

# อ่านค่า Gemini API Keys (รองรับหลายคีย์)
raw_keys = os.environ.get('GEMINI_API_KEYS', '')
GEMINI_API_KEYS = [k.strip() for k in raw_keys.split(',') if k.strip()]
current_key_index = 0

# ตัวแปรระบบ AI
MAX_HISTORY_LENGTH = 10
conversation_history: Dict[int, List[Dict[str, str]]] = {}
user_tasks: Dict[int, asyncio.Task] = {}
key_rotation_lock = asyncio.Lock()

# ตัวแปรเก็บค่า Persona
CURRENT_PERSONA = "CASUAL"

# Init Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 2. Helper Functions ---

def is_admin(user_id):
    return user_id in BOT_OWNER_IDS

def clean_discord_message(text: str) -> str:
    text = re.sub(r'<@!?\d+>', '', text)
    text = re.sub(r'<@&\d+>', '', text)
    text = re.sub(r'<#\d+>', '', text)
    text = re.sub(r'<a?:.+?:\d+>', '', text)
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def get_ai_system_instruction(mode: str, user_text: str) -> str:
    user_text_lower = user_text.lower()
    bot_gender = "หญิง"
    
    if 'ครับ' in user_text_lower or 'คับ' in user_text_lower:
        bot_gender = "หญิง (แฟนสาว/เพื่อนสาว)" 
    elif 'ค่ะ' in user_text_lower or 'คะ' in user_text_lower:
        bot_gender = "ชาย (แฟนหนุ่ม/เพื่อนชาย)"
    
    common_style = (
        "IMPORTANT: "
        "1. ตอบกลับเหมือนคนคุยแชท (Casual) ห้ามตอบเป็น Robot หรือเรียงความ "
        "2. ความยาว: 'ระดับกลาง (Medium)' ประมาณ 1-3 ประโยค กำลังดี "
        "3. ตัดคำเยิ่นเย้อ เข้าประเด็น ห้ามใช้ Bullet Point ถ้าไม่จำเป็น "
    )

    instructions = {
        "LOVER": (
            f"{common_style} "
            f"บทบาท: แฟนขี้อ้อน (เพศ: {bot_gender}) "
            "แทนตัวเองว่า 'เค้า' แทนคู่สนทนาว่า 'เตง', 'ที่รัก' "
            "คุยเสียงอ้อนๆ งอแงนิดๆ พอน่ารัก ไม่ต้องยาวมาก เน้นความรู้สึก"
        ),
        "RUDE": (
            f"{common_style} "
            "บทบาท: เพื่อนปากหมา "
            "ใช้คำหยาบได้ (กู/มึง/เหี้ย) ตามบริบทเพื่อนสนิท "
            "ไม่ต้องเกรงใจ ตอบกวนตีน สั้นๆ ได้ใจความ"
        ),
        "FUNNY": (
            f"{common_style} "
            "บทบาท: เพื่อนสายฮา "
            "เน้นยิงมุข ตลกโปกฮา ขำง่าย (555+) "
            "คุยเป็นกันเอง สร้างบรรยากาศสนุกสนาน"
        ),
        "CASUAL": (
            f"{common_style} "
            "บทบาท: เพื่อนคุยเล่น (Chill Guy) "
            "คุยง่ายๆ สบายๆ เหมือนเพื่อนคุยกัน "
            "ไม่ต้องทางการมาก ให้ข้อมูลได้แต่ใช้ภาษาปาก"
        )
    }
    return instructions.get(mode, instructions["CASUAL"])

# --- 3. Key Check Logic ---
async def run_key_check_diagnostic():
    embed = discord.Embed(title="🔑 สถานะ Gemini API Keys", description=f"ตรวจสอบ {len(GEMINI_API_KEYS)} Keys", color=discord.Color.blue())
    valid_count = 0
    invalid_count = 0

    for i, key in enumerate(GEMINI_API_KEYS):
        masked = key[:4] + "..." + key[-4:]
        start_time = time_module.time()
        try:
            client = genai.Client(api_key=key)
            await asyncio.to_thread(client.models.generate_content, model='gemini-2.0-flash-exp', contents='Ping')
            latency = (time_module.time() - start_time) * 1000
            status_icon = "🟢 Active" if latency < 1500 else "🟡 Slow"
            embed.add_field(name=f"Key #{i+1}", value=f"Stat: {status_icon}\nPing: {latency:.0f}ms\nKey: {masked}", inline=False)
            valid_count += 1
        except Exception as e:
            embed.add_field(name=f"Key #{i+1}", value=f"🔴 Error: {str(e)[:30]}\nKey: {masked}", inline=False)
            invalid_count += 1
            
    embed.set_footer(text=f"ใช้งานได้ {valid_count} / เสีย {invalid_count}")
    return embed

# --- 4. UI & Views ---

class PersonaSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="💖 แฟน/คนรัก (Lover)", value="LOVER", emoji="😘"),
            discord.SelectOption(label="🤬 เพื่อนปากหมา (Rude)", value="RUDE", emoji="🖕"),
            discord.SelectOption(label="🤡 สายฮา (Funny)", value="FUNNY", emoji="🤣"),
            discord.SelectOption(label="😎 คุยเล่นทั่วไป (Casual)", value="CASUAL", emoji="🤓"),
        ]
        super().__init__(placeholder="เลือกบุคลิก AI...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user.id):
             return await interaction.response.send_message("❌ เฉพาะ Admin", ephemeral=True)
        
        global CURRENT_PERSONA
        CURRENT_PERSONA = self.values[0]
        
        msg_map = {
            "LOVER": "💖 **เปลี่ยนโหมด: แฟนขี้อ้อน** (งื้อออ คิดถึงเค้าไหม~)",
            "RUDE": "🤬 **เปลี่ยนโหมด: ปากหมา** (มองไร มีปัญหาปะ?)",
            "FUNNY": "🤡 **เปลี่ยนโหมด: สายฮา** (พร้อมยิงมุขละครับ!)",
            "CASUAL": "😎 **เปลี่ยนโหมด: ทั่วไป** (โอเค คุยกันชิลๆ)"
        }
        await interaction.response.send_message(msg_map.get(CURRENT_PERSONA, "Changed"), ephemeral=True)

class PersonaView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(PersonaSelect())

class AIMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎭 เปลี่ยนบุคลิก AI", style=discord.ButtonStyle.primary, custom_id="btn_ai_persona")
    async def btn_persona(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id): return await interaction.response.send_message("❌ เฉพาะ Admin", ephemeral=True)
        await interaction.response.send_message("เลือกบุคลิกที่ต้องการ:", view=PersonaView(), ephemeral=True)

    @discord.ui.button(label="🔑 เช็คสถานะ Keys", style=discord.ButtonStyle.secondary, custom_id="btn_ai_keys")
    async def btn_keys(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user.id): return await interaction.response.send_message("❌ เฉพาะ Admin", ephemeral=True)
        await interaction.response.send_message("🕵️‍♂️ **กำลังตรวจสอบ Key...**", ephemeral=True)
        report = await run_key_check_diagnostic()
        await interaction.followup.send(embed=report, ephemeral=True)

# --- 5. Main Chat Logic ---

async def process_ai_chat_request(msg: discord.Message):
    global current_key_index
    user_id = msg.author.id
    current_prompt = clean_discord_message(msg.content)
    
    if not current_prompt or not GEMINI_API_KEYS: return

    sys_instruction = get_ai_system_instruction(CURRENT_PERSONA, current_prompt)
    
    history = conversation_history.get(user_id, [])
    contents = []
    for turn in history:
        if turn.get('user'): contents.append(types.Content(role='user', parts=[types.Part(text=turn['user'])]))
        if turn.get('model'): contents.append(types.Content(role='model', parts=[types.Part(text=turn['model'])]))
    contents.append(types.Content(role='user', parts=[types.Part(text=current_prompt)]))

    async with msg.channel.typing():
        success = False
        start_index = 0
        async with key_rotation_lock: start_index = current_key_index

        for i in range(len(GEMINI_API_KEYS)):
            target_index = (start_index + i) % len(GEMINI_API_KEYS)
            key = GEMINI_API_KEYS[target_index]
            try:
                client = genai.Client(api_key=key)
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model='gemini-2.0-flash-exp',
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=sys_instruction)
                )
                ans = response.text or "..."
                
                await msg.reply(ans[:1900])
                
                conversation_history.setdefault(user_id, []).append({'user': current_prompt, 'model': ans})
                if len(conversation_history[user_id]) > MAX_HISTORY_LENGTH:
                    conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY_LENGTH:]
                
                success = True
                async with key_rotation_lock: current_key_index = (target_index + 1) % len(GEMINI_API_KEYS)
                break
            except Exception as e:
                print(f"Key #{target_index} Error: {e}")
                continue
        
        if not success:
            await msg.reply("😵‍💫 ระบบมึนงง (API Error ทั้งหมด)")

        if user_id in user_tasks: del user_tasks[user_id]

# --- 6. Commands & Events ---

@bot.event
async def on_ready():
    print(f"✅ AI Bot Online: {bot.user}")
    print(f"✅ Keys Loaded: {len(GEMINI_API_KEYS)}")
    print(f"✅ Port: {os.environ.get('PORT', '8000')}")

@bot.command(name='menu', aliases=['เมนู'])
async def show_ai_menu(ctx):
    if not is_admin(ctx.author.id): return
    embed = discord.Embed(title="🤖 AI Control Panel", description="ตั้งค่าระบบ AI", color=discord.Color.gold())
    await ctx.send(embed=embed, view=AIMenuView())

@bot.event
async def on_message(msg):
    if msg.author.bot: return
    
    is_mentioned = bot.user in msg.mentions
    is_query_channel = (QUERY_CHANNEL_ID != 0 and msg.channel.id == QUERY_CHANNEL_ID)
    
    if msg.content.startswith(bot.command_prefix):
        await bot.process_commands(msg)
        return

    if (is_mentioned or is_query_channel):
        if msg.author.id not in user_tasks:
            task = bot.loop.create_task(process_ai_chat_request(msg))
            user_tasks[msg.author.id] = task

# --- 7. Web Server ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 AI Discord Bot is Running on Koyeb!", 200

@app.route('/health')
def health():
    return {"status": "healthy", "bot": str(bot.user)}, 200

def run_web():
    port = int(os.environ.get('PORT', 8000))
    print(f"🌐 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port)

def start_server():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()

# --- 8. Main ---
if __name__ == "__main__":
    if not os.environ.get('DISCORD_BOT_TOKEN'):
        print("❌ Error: ไม่พบ DISCORD_BOT_TOKEN")
        exit(1)
    
    if not GEMINI_API_KEYS:
        print("❌ Error: ไม่พบ GEMINI_API_KEYS")
        exit(1)
    
    print(f"✅ Loaded {len(GEMINI_API_KEYS)} Gemini API Keys")
    print(f"✅ Loaded {len(BOT_OWNER_IDS)} Admin IDs")
    
    start_server()
    
    try:
        bot.run(os.environ.get('DISCORD_BOT_TOKEN'))
    except Exception as e:
        print(f"❌ Bot Error: {e}")

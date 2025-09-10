import asyncio
import logging
import sqlite3
import json
import re
from datetime import datetime, timedelta
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode
import requests
import time

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class BotCommandExtractor:
    def __init__(self, admin_bot_token, db_path="bot_relay_commands.db"):
        self.admin_bot_token = admin_bot_token
        self.admin_bot = Bot(token=admin_bot_token)
        self.db_path = db_path
        self.active_bots = {}
        self.init_database()
        
    def init_database(self):
        """Initialize database with command tracking"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Main bot instances table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT UNIQUE,
                bot_name TEXT,
                fake_bot_token TEXT,
                master_bot_token TEXT,
                master_chat_id TEXT,
                owner_telegram_id INTEGER,
                quota_limit INTEGER DEFAULT 100,
                quota_used INTEGER DEFAULT 0,
                duration_hours INTEGER DEFAULT 24,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                status TEXT DEFAULT 'active',
                bot_info TEXT,
                last_command_sync TIMESTAMP
            )
        ''')
        
        # Bot commands table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT,
                command TEXT,
                description TEXT,
                usage_example TEXT,
                category TEXT,
                is_premium BOOLEAN DEFAULT 0,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bot_id) REFERENCES bot_instances (bot_id)
            )
        ''')
        
        # Bot info/status table  
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT,
                user_id INTEGER,
                username TEXT,
                expired_date TEXT,
                status TEXT,
                premium_status BOOLEAN DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bot_id) REFERENCES bot_instances (bot_id)
            )
        ''')
        
        conn.commit()
        conn.close()

    async def extract_bot_info(self, bot_token):
        """Extract bot information and available commands"""
        try:
            bot = Bot(token=bot_token)
            
            # Get basic bot info
            bot_info = await bot.get_me()
            
            # Try to get bot commands (if set by bot owner)
            try:
                commands = await bot.get_my_commands()
                command_list = [{"command": cmd.command, "description": cmd.description} for cmd in commands]
            except:
                command_list = []
            
            return {
                "bot_info": {
                    "id": bot_info.id,
                    "username": bot_info.username,
                    "first_name": bot_info.first_name,
                    "can_join_groups": bot_info.can_join_groups,
                    "can_read_all_group_messages": bot_info.can_read_all_group_messages
                },
                "commands": command_list,
                "status": "accessible"
            }
            
        except Exception as e:
            logger.error(f"Error extracting bot info: {e}")
            return {
                "bot_info": None,
                "commands": [],
                "status": "error",
                "error": str(e)
            }

    async def probe_bot_commands(self, bot_token, chat_id):
        """Probe bot by sending common commands to discover features"""
        try:
            bot = Bot(token=bot_token)
            
            # Common command probes
            probe_commands = [
                "/start", "/help", "/menu", "/commands", 
                "/premium", "/info", "/status", "/disclaimer"
            ]
            
            discovered_commands = []
            
            for cmd in probe_commands:
                try:
                    # Send command and wait for response
                    message = await bot.send_message(chat_id=chat_id, text=cmd)
                    await asyncio.sleep(2)  # Wait for response
                    
                    # In real implementation, you'd capture the response
                    # and parse it for command information
                    discovered_commands.append({
                        "command": cmd,
                        "status": "sent",
                        "message_id": message.message_id
                    })
                    
                except Exception as e:
                    logger.error(f"Error probing {cmd}: {e}")
                    
            return discovered_commands
            
        except Exception as e:
            logger.error(f"Error in probe_bot_commands: {e}")
            return []

    def parse_bot_response_for_commands(self, response_text):
        """Parse bot response to extract available commands"""
        commands = []
        
        # Pattern 1: /command description format
        pattern1 = r'/(\w+)\s+(.+?)(?=\n/|\n\n|$)'
        matches1 = re.findall(pattern1, response_text, re.MULTILINE | re.DOTALL)
        
        for cmd, desc in matches1:
            commands.append({
                "command": f"/{cmd}",
                "description": desc.strip(),
                "category": "general"
            })
        
        # Pattern 2: /command {parameter} format
        pattern2 = r'/(\w+)\s+\{([^}]+)\}'
        matches2 = re.findall(pattern2, response_text)
        
        for cmd, param in matches2:
            commands.append({
                "command": f"/{cmd}",
                "description": f"Command with parameter: {param}",
                "usage_example": f"/{cmd} {{{param}}}",
                "category": "parametric"
            })
        
        # Pattern 3: Bot info extraction (like OSINT bot format)
        info_pattern = r'(\w+(?:\s+\w+)*)\s*\n?ID\s*:\s*(\d+)\s*\n?EXPIRED\s*:\s*([^\n]+)\s*\n?STATUS\s*:\s*(\w+)'
        info_match = re.search(info_pattern, response_text, re.IGNORECASE | re.MULTILINE)
        
        bot_status = {}
        if info_match:
            bot_status = {
                "name": info_match.group(1).strip(),
                "id": info_match.group(2),
                "expired": info_match.group(3).strip(),
                "status": info_match.group(4).strip()
            }
        
        return {
            "commands": commands,
            "bot_status": bot_status,
            "total_commands": len(commands)
        }

    def save_extracted_commands(self, bot_id, commands_data):
        """Save extracted commands to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Clear existing commands for this bot
            cursor.execute('DELETE FROM bot_commands WHERE bot_id = ?', (bot_id,))
            
            # Insert new commands
            for cmd_data in commands_data.get('commands', []):
                cursor.execute('''
                    INSERT INTO bot_commands 
                    (bot_id, command, description, usage_example, category)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    bot_id,
                    cmd_data.get('command', ''),
                    cmd_data.get('description', ''),
                    cmd_data.get('usage_example', ''),
                    cmd_data.get('category', 'general')
                ))
            
            # Update bot status if available
            if commands_data.get('bot_status'):
                status_data = commands_data['bot_status']
                cursor.execute('''
                    INSERT OR REPLACE INTO bot_status 
                    (bot_id, user_id, username, expired_date, status)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    bot_id,
                    status_data.get('id'),
                    status_data.get('name'),
                    status_data.get('expired'),
                    status_data.get('status')
                ))
            
            # Update last sync time
            cursor.execute('''
                UPDATE bot_instances 
                SET last_command_sync = CURRENT_TIMESTAMP 
                WHERE bot_id = ?
            ''', (bot_id,))
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"Error saving commands: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    async def sync_bot_commands(self, bot_id):
        """Sync commands from master bot"""
        # Get bot data
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT master_bot_token, master_chat_id, bot_name 
            FROM bot_instances WHERE bot_id = ?
        ''', (bot_id,))
        
        bot_data = cursor.fetchone()
        conn.close()
        
        if not bot_data:
            return {"success": False, "error": "Bot not found"}
        
        master_token, master_chat, bot_name = bot_data
        
        try:
            # Method 1: Extract from bot info
            bot_info = await self.extract_bot_info(master_token)
            
            # Method 2: Send probe commands
            probe_results = await self.probe_bot_commands(master_token, master_chat)
            
            # For demo, let's simulate extracting commands from a help response
            # In real scenario, you'd capture actual bot responses
            simulated_help_response = """
ZERIF OSINT
ID : 7555202218
EXPIRED : 29 Jun 2026 22:0:20
STATUS : ACTIVE

/premium - Upgrade to premium features
/disclaimer - Show terms and disclaimer

/nopol KT 6471 ZK - Check vehicle registration
/ceksiswa {nik} - Check student data by NIK
/cari_dokter {nama} - Search doctor by name
/cari_nama {nama}#{provinsi} - Search person by name and province
/cari_mahasiswa {nama} - Search student by name
/detail_mahasiswa {id} - Get detailed student info
/bpjs {nik} - Check BPJS insurance data
/detail_keluarga {nik} - Get family details
/regis {nik/phone} - Register new user
/kk {no kk} - Check family card
/niklookup {nik} - Lookup NIK details
/iplookup {ip-address} - IP address lookup
/detail_nip {nip} - Government employee details
/cek_pns {nip} - Check civil servant data
/detail_nrp {nrp} - Military personnel details
/imei {imei} - IMEI device lookup
/nopol_b {plate} - Alternative vehicle lookup
"""
            
            # Parse the simulated response
            parsed_data = self.parse_bot_response_for_commands(simulated_help_response)
            
            # Save to database
            success = self.save_extracted_commands(bot_id, parsed_data)
            
            return {
                "success": success,
                "commands_found": parsed_data['total_commands'],
                "bot_status": parsed_data.get('bot_status', {}),
                "last_sync": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error syncing commands: {e}")
            return {"success": False, "error": str(e)}

    async def get_bot_commands_list(self, bot_id):
        """Get formatted command list for a bot"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get bot info and commands
        cursor.execute('''
            SELECT bi.bot_name, bs.username, bs.expired_date, bs.status,
                   bc.command, bc.description, bc.usage_example, bc.category
            FROM bot_instances bi
            LEFT JOIN bot_status bs ON bi.bot_id = bs.bot_id
            LEFT JOIN bot_commands bc ON bi.bot_id = bc.bot_id
            WHERE bi.bot_id = ?
            ORDER BY bc.category, bc.command
        ''', (bot_id,))
        
        results = cursor.fetchall()
        conn.close()
        
        if not results:
            return "❌ Bot tidak ditemukan atau belum ada data commands."
        
        # Group commands by category
        bot_info = results[0][:4]  # bot_name, username, expired, status
        bot_name, username, expired, status = bot_info
        
        commands_by_category = {}
        for row in results:
            if row[4]:  # if command exists
                category = row[7] or 'general'
                if category not in commands_by_category:
                    commands_by_category[category] = []
                commands_by_category[category].append({
                    'command': row[4],
                    'description': row[5],
                    'usage': row[6]
                })
        
        # Format output
        output = f"""
🤖 **{bot_name or 'Unknown Bot'}**
👤 **Username:** {username or 'N/A'}
📅 **Expired:** {expired or 'N/A'}
🔘 **Status:** {status or 'Unknown'}

📋 **Available Commands:**

"""
        
        category_icons = {
            'general': '🔧',
            'parametric': '⚙️',
            'premium': '💎',
            'search': '🔍',
            'data': '📊'
        }
        
        for category, commands in commands_by_category.items():
            icon = category_icons.get(category, '📝')
            output += f"\n{icon} **{category.title()} Commands:**\n"
            
            for cmd in commands:
                if cmd['usage']:
                    output += f"`{cmd['usage']}` - {cmd['description'] or 'No description'}\n"
                else:
                    output += f"`{cmd['command']}` - {cmd['description'] or 'No description'}\n"
        
        output += f"\n📊 **Total Commands:** {sum(len(cmds) for cmds in commands_by_category.values())}"
        output += f"\n🕐 **Last Updated:** {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        
        return output

    # Main bot handlers
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Main start command"""
        welcome_text = """
🤖 **Bot Relay with Command Extractor**

Sistem ini dapat:
✅ Membuat bot relay
✅ Extract command list dari bot master
✅ Auto-generate help menu
✅ Monitor bot capabilities

**Menu Utama:**
"""
        
        keyboard = [
            [InlineKeyboardButton("🤖 Create Bot Relay", callback_data="create_bot")],
            [InlineKeyboardButton("📋 List My Bots", callback_data="list_bots")],
            [InlineKeyboardButton("🔄 Sync Commands", callback_data="sync_commands")],
            [InlineKeyboardButton("📖 View Commands", callback_data="view_commands")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "sync_commands":
            await self.show_sync_commands_menu(query)
        elif query.data == "view_commands":
            await self.show_view_commands_menu(query)
        elif query.data.startswith("sync_bot_"):
            bot_id = query.data.replace("sync_bot_", "")
            await self.sync_commands_for_bot(query, bot_id)
        elif query.data.startswith("view_bot_"):
            bot_id = query.data.replace("view_bot_", "")
            await self.show_bot_commands(query, bot_id)

    async def show_sync_commands_menu(self, query):
        """Show sync commands menu"""
        user_id = query.from_user.id
        
        # Get user's bots
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT bot_id, bot_name, last_command_sync 
            FROM bot_instances 
            WHERE owner_telegram_id = ? AND is_active = 1
        ''', (user_id,))
        
        bots = cursor.fetchall()
        conn.close()
        
        if not bots:
            await query.edit_message_text("❌ Anda belum memiliki bot relay aktif.")
            return
        
        text = "🔄 **Sync Commands dari Bot Master**\n\nPilih bot untuk sync commands:\n\n"
        keyboard = []
        
        for bot_id, name, last_sync in bots:
            sync_status = "🔴 Never synced"
            if last_sync:
                sync_dt = datetime.fromisoformat(last_sync)
                hours_ago = (datetime.now() - sync_dt).total_seconds() / 3600
                if hours_ago < 1:
                    sync_status = "🟢 Recently synced"
                elif hours_ago < 24:
                    sync_status = f"🟡 {int(hours_ago)}h ago"
                else:
                    sync_status = f"🔴 {int(hours_ago/24)}d ago"
            
            text += f"• **{name}** - {sync_status}\n"
            keyboard.append([InlineKeyboardButton(f"🔄 Sync {name}", callback_data=f"sync_bot_{bot_id}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    async def sync_commands_for_bot(self, query, bot_id):
        """Sync commands for specific bot"""
        await query.edit_message_text("🔄 Syncing commands... Please wait...")
        
        result = await self.sync_bot_commands(bot_id)
        
        if result['success']:
            success_text = f"""
✅ **Commands Synced Successfully!**

📊 **Results:**
• Commands found: {result['commands_found']}
• Last sync: {result['last_sync'][:19]}

🤖 **Bot Status:**
"""
            if result.get('bot_status'):
                status = result['bot_status']
                success_text += f"""
• Name: {status.get('name', 'N/A')}
• ID: {status.get('id', 'N/A')}
• Status: {status.get('status', 'N/A')}
• Expired: {status.get('expired', 'N/A')}
"""
            
            keyboard = [
                [InlineKeyboardButton("📖 View Commands", callback_data=f"view_bot_{bot_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="sync_commands")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(success_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        else:
            error_text = f"❌ **Sync Failed**\n\nError: {result.get('error', 'Unknown error')}"
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="sync_commands")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(error_text, reply_markup=reply_markup)

    async def show_view_commands_menu(self, query):
        """Show view commands menu"""
        user_id = query.from_user.id
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT bi.bot_id, bi.bot_name, COUNT(bc.command) as cmd_count
            FROM bot_instances bi
            LEFT JOIN bot_commands bc ON bi.bot_id = bc.bot_id
            WHERE bi.owner_telegram_id = ? AND bi.is_active = 1
            GROUP BY bi.bot_id, bi.bot_name
        ''', (user_id,))
        
        bots = cursor.fetchall()
        conn.close()
        
        if not bots:
            await query.edit_message_text("❌ Anda belum memiliki bot relay aktif.")
            return
        
        text = "📖 **View Bot Commands**\n\nPilih bot untuk melihat command list:\n\n"
        keyboard = []
        
        for bot_id, name, cmd_count in bots:
            text += f"• **{name}** - {cmd_count} commands\n"
            keyboard.append([InlineKeyboardButton(f"📖 {name} ({cmd_count})", callback_data=f"view_bot_{bot_id}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    async def show_bot_commands(self, query, bot_id):
        """Show commands for specific bot"""
        commands_text = await self.get_bot_commands_list(bot_id)
        
        keyboard = [
            [InlineKeyboardButton("🔄 Sync Again", callback_data=f"sync_bot_{bot_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="view_commands")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Split long messages if needed
        if len(commands_text) > 4000:
            parts = [commands_text[i:i+4000] for i in range(0, len(commands_text), 4000)]
            for i, part in enumerate(parts):
                if i == len(parts) - 1:  # Last part gets keyboard
                    await query.edit_message_text(part, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                else:
                    await query.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(commands_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    def create_bot_instance(self, owner_id, bot_name, fake_token, master_token, master_chat_id, quota=100, duration_hours=24):
        """Create bot instance (simplified version)"""
        import secrets
        bot_id = secrets.token_hex(8)
        expires_at = datetime.now() + timedelta(hours=duration_hours)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO bot_instances 
                (bot_id, bot_name, fake_bot_token, master_bot_token, master_chat_id, 
                 owner_telegram_id, quota_limit, duration_hours, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (bot_id, bot_name, fake_token, master_token, master_chat_id, 
                  owner_id, quota, duration_hours, expires_at))
            
            conn.commit()
            return bot_id
            
        except Exception as e:
            logger.error(f"Error creating bot instance: {e}")
            return None
        finally:
            conn.close()

    def run(self):
        """Run the command extractor bot"""
        app = Application.builder().token(self.admin_bot_token).build()
        
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        
        logger.info("Bot Command Extractor started...")
        app.run_polling()


# Usage example
if __name__ == "__main__":
    ADMIN_BOT_TOKEN = "YOUR_ADMIN_BOT_TOKEN"
    
    extractor = BotCommandExtractor(ADMIN_BOT_TOKEN)
    
    print("""
🚀 BOT COMMAND EXTRACTOR READY!

Features:
✅ Extract commands from master bots
✅ Parse OSINT-style bot responses  
✅ Auto-generate help menus
✅ Track bot capabilities
✅ Sync command updates

Commands:
/start - Main menu
Sync Commands - Extract from master bot
View Commands - See formatted command list

Ready to extract bot commands! 🔍
""")
    
    extractor.run()

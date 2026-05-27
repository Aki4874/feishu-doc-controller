"""
飞书文档控制器 AstrBot 插件 - 核心模块

支持通过飞书开放平台 API 对飞书文档进行全生命周期管理，
包括文档的搜索、读取、创建、写入，以及知识库管理、权限管理、
多维表格、电子表格、幻灯片、画板等操作。
所有功能支持 AI 自动调用（通过 FunctionTool 机制）。
"""

import aiohttp
import json
import time
import re
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext


# ============================================================================
# FeishuConfig — 飞书应用配置
# ============================================================================

@dataclass
class FeishuConfig:
    """飞书应用配置数据类"""
    app_id: str       # 飞书应用 App ID
    app_secret: str   # 飞书应用 App Secret


# ============================================================================
# FeishuAPIClient — 异步飞书 API 客户端
# ============================================================================

class FeishuAPIClient:
    """异步飞书开放平台 API 客户端，封装所有飞书 API 调用。

    特性：
    - Token 内存缓存 + 过期前 300 秒自动刷新
    - 所有请求统一使用 aiohttp 异步调用
    - 完整的类型注解和异常处理
    """

    def __init__(self, config: FeishuConfig):
        """初始化 API 客户端。

        Args:
            config: 飞书应用配置，包含 app_id 和 app_secret
        """
        self.config = config
        self.base_url = "https://open.feishu.cn/open-apis"
        self.tenant_access_token: Optional[str] = None
        self.token_expire_time: float = 0

    # -----------------------------------------------------------------------
    # Token 管理
    # -----------------------------------------------------------------------

    async def get_tenant_access_token(self) -> str:
        """获取并缓存 Tenant Access Token，过期前 300 秒自动刷新。

        Returns:
            有效的 tenant_access_token 字符串

        Raises:
            Exception: 如果未配置 App ID/Secret 或 API 返回错误
        """
        # 检查缓存是否有效
        if self.tenant_access_token and time.time() < (self.token_expire_time - 300):
            return self.tenant_access_token

        # 验证配置
        if not self.config.app_id or not self.config.app_secret:
            raise Exception("未配置 App ID 或 App Secret，请在管理面板中填写")

        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                result = await resp.json()

        if not isinstance(result, dict):
            raise Exception(f"获取 Token 失败: API 返回了非预期的数据类型 {type(result).__name__}")

        code = result.get('code')
        if code == 0:
            self.tenant_access_token = result.get('tenant_access_token', '')
            if not self.tenant_access_token:
                raise Exception("获取 Token 失败: 响应中缺少 tenant_access_token")
            self.token_expire_time = time.time() + result.get('expire', 7200)
            logger.info("Tenant Access Token 获取成功")
            return self.tenant_access_token
        elif code == 10003:
            raise Exception("App ID 或 App Secret 无效，请检查配置")
        elif code == 10014:
            raise Exception("App ID 格式错误，应以'cli_'开头")
        else:
            raise Exception(f"获取 Token 失败: {result.get('msg', '未知错误')} (code: {code})")

    # -----------------------------------------------------------------------
    # 通用请求辅助方法
    # -----------------------------------------------------------------------

    async def _get(self, path: str, params: dict = None) -> dict:
        """发送 GET 请求并处理通用错误。

        Args:
            path: API 路径（不含 base_url）
            params: 查询参数

        Returns:
            API 响应的 data 字段
        """
        token = await self.get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                result = await resp.json()
                if not isinstance(result, dict):
                    raise Exception(f"API 返回非 JSON 对象: {type(result).__name__}")
                return result

    async def _post(self, path: str, data: dict = None, params: dict = None) -> dict:
        """发送 POST 请求并处理通用错误。

        Args:
            path: API 路径（不含 base_url）
            data: JSON 请求体
            params: 查询参数

        Returns:
            API 响应的 data 字段
        """
        token = await self.get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data, params=params) as resp:
                result = await resp.json()
                if not isinstance(result, dict):
                    raise Exception(f"API 返回非 JSON 对象: {type(result).__name__}")
                return result

    async def _patch(self, path: str, data: dict = None, params: dict = None) -> dict:
        """发送 PATCH 请求。

        Args:
            path: API 路径（不含 base_url）
            data: JSON 请求体
            params: 查询参数

        Returns:
            API 响应的 data 字段
        """
        token = await self.get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, headers=headers, json=data, params=params) as resp:
                result = await resp.json()
                if not isinstance(result, dict):
                    raise Exception(f"API 返回非 JSON 对象: {type(result).__name__}")
                return result

    async def _delete(self, path: str, params: dict = None) -> dict:
        """发送 DELETE 请求。

        Args:
            path: API 路径（不含 base_url）
            params: 查询参数

        Returns:
            API 响应的 data 字段
        """
        token = await self.get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers, params=params) as resp:
                result = await resp.json()
                if not isinstance(result, dict):
                    raise Exception(f"API 返回非 JSON 对象: {type(result).__name__}")
                return result

    def _check_result(self, result: dict, operation: str) -> dict:
        """检查 API 返回结果，code==0 时返回 data，否则抛异常。

        Args:
            result: API 原始响应
            operation: 操作描述（用于错误信息）

        Returns:
            result['data']
        """
        if not isinstance(result, dict):
            raise Exception(f"{operation}失败: API 返回了非预期的数据类型 {type(result).__name__}，原始值: {str(result)[:200]}")
        code = result.get('code')
        if code == 0:
            return result.get('data', {})
        raise Exception(f"{operation}失败: {result.get('msg', '未知错误')} (code: {code})")

    # -----------------------------------------------------------------------
    # 文档 ID 提取
    # -----------------------------------------------------------------------

    def _extract_document_id(self, input_str: str) -> str:
        """从输入中提取文档 ID，支持多种格式。

        支持格式：
        - 纯文本 ID：doxcnxxxxx 或 WR4FwM2WiiXjVHkJzo0cXTpen7b
        - 完整 URL：https://my.feishu.cn/wiki/WR4FwM2WiiXjVHkJzo0cXTpen7b
        - 完整 URL：https://xxx.feishu.cn/docx/doxcnxxxxx

        Args:
            input_str: 用户输入的文档 ID 或 URL

        Returns:
            提取的文档 ID
        """
        if not input_str:
            return ""

        input_str = input_str.strip()

        # 如果包含 URL，解析路径
        if "http" in input_str or "feishu.cn" in input_str:
            # 去掉 URL 参数
            if "?" in input_str:
                input_str = input_str.split("?")[0]
            # 分割路径
            parts = input_str.rstrip("/").split("/")
            # 保留词（非文档 ID 的路径段）
            skip_words = {"wiki", "docx", "docs", "sheet", "base", "bitable"}
            # 从后往前找第一个不在保留词中的路径段作为 ID
            for part in reversed(parts):
                if part and part not in skip_words:
                    return part

        return input_str

    # -----------------------------------------------------------------------
    # 文本提取工具（从文档块中提取纯文本）
    # -----------------------------------------------------------------------

    def _extract_text_from_elements(self, elements: List[Dict]) -> str:
        """从元素列表中提取 text_run.content，用空字符串 join。

        Args:
            elements: 飞书文档元素列表

        Returns:
            拼接后的纯文本
        """
        texts = []
        for elem in elements:
            text_run = elem.get('text_run')
            if text_run:
                content = text_run.get('content', '')
                if content:
                    texts.append(content)
        return "".join(texts)

    def extract_text_from_blocks(self, blocks: List[Dict]) -> str:
        """遍历文档块，根据 block_type 提取纯文本内容。

        支持所有常见飞书文档块类型：文本、标题、列表、代码块、
        引用、待办、表格、多维表格、电子表格、画板等。

        Args:
            blocks: 飞书文档块列表

        Returns:
            提取的纯文本，块之间用 \\n\\n 分隔
        """
        lines = []
        for block in blocks:
            block_type = block.get('block_type', 0)

            if block_type == 2:  # 文本块
                text = block.get('text', {})
                elements = text.get('elements', [])
                lines.append(self._extract_text_from_elements(elements))

            elif 3 <= block_type <= 11:  # 标题 heading1-9
                level = block_type - 2
                prefix = "#" * level + " "
                field_map = {3: 'heading1', 4: 'heading2', 5: 'heading3',
                             6: 'heading4', 7: 'heading5', 8: 'heading6',
                             9: 'heading7', 10: 'heading8', 11: 'heading9'}
                heading_data = block.get(field_map[block_type], {})
                elements = heading_data.get('elements', [])
                lines.append(prefix + self._extract_text_from_elements(elements))

            elif block_type == 12:  # 无序列表
                bullet = block.get('bullet', {})
                elements = bullet.get('elements', [])
                lines.append("- " + self._extract_text_from_elements(elements))

            elif block_type == 13:  # 有序列表
                ordered = block.get('ordered', {})
                elements = ordered.get('elements', [])
                lines.append("1. " + self._extract_text_from_elements(elements))

            elif block_type == 14:  # 代码块
                code = block.get('code', {})
                elements = code.get('elements', [])
                lang = code.get('style', {}).get('language', '')
                text = self._extract_text_from_elements(elements)
                lines.append(f"```{lang}\n{text}\n```")

            elif block_type == 15:  # 引用
                quote = block.get('quote', {})
                elements = quote.get('elements', [])
                lines.append("> " + self._extract_text_from_elements(elements))

            elif block_type == 17:  # 待办事项
                todo = block.get('todo', {})
                elements = todo.get('elements', [])
                done = todo.get('style', {}).get('done', False)
                prefix = "[x] " if done else "[ ] "
                lines.append(prefix + self._extract_text_from_elements(elements))

            elif block_type == 18:  # 分割线
                lines.append("---")

            elif block_type == 19:  # 高亮块 callout
                callout = block.get('callout', {})
                elements = callout.get('elements', [])
                lines.append("💡 " + self._extract_text_from_elements(elements))

            elif block_type == 20:  # 表格
                table = block.get('table', {})
                row_count = table.get('property', {}).get('row_size', 0)
                col_count = table.get('property', {}).get('column_size', 0)
                lines.append(f"[表格: {row_count}行 x {col_count}列]")

            elif block_type == 21:  # 单元格
                cell = block.get('table_cell', {})
                elements = cell.get('elements', [])
                lines.append(self._extract_text_from_elements(elements))

            elif block_type == 22:  # 多维表格 bitable
                bitable = block.get('bitable', {})
                token = bitable.get('token', '')
                lines.append(f"[多维表格: {token}]")

            elif block_type == 23:  # 电子表格 sheet
                sheet = block.get('sheet', {})
                token = sheet.get('token', '')
                lines.append(f"[电子表格: {token}]")

            elif block_type == 24:  # 思维笔记 mindnote
                mindnote = block.get('mindnote', {})
                token = mindnote.get('token', '')
                lines.append(f"[思维笔记: {token}]")

            elif block_type in (25, 26):  # 分栏
                lines.append("[分栏]")

            elif block_type == 27:  # 图片
                lines.append("[图片]")

            elif block_type == 28:  # 流程图 diagram
                diagram = block.get('diagram', {})
                token = diagram.get('token', '')
                lines.append(f"[流程图: {token}]")

            elif block_type == 29:  # 文件 file
                file_block = block.get('file', {})
                name = file_block.get('name', '')
                lines.append(f"[文件: {name}]")

            elif block_type == 30:  # 内嵌 iframe
                lines.append("[内嵌内容]")

            elif block_type == 43:  # 画板
                board = block.get('board', {})
                board_token = board.get('token', '')
                board_width = board.get('width', 0)
                board_height = board.get('height', 0)
                size_info = f" {board_width}x{board_height}" if board_width and board_height else ""
                lines.append(f"[画板{size_info}: {board_token}]")

        return "\n\n".join(lines)

    # -----------------------------------------------------------------------
    # 内联富文本解析（Markdown → 飞书 text_run 元素）
    # -----------------------------------------------------------------------

    def _parse_inline_elements(self, text: str) -> List[Dict]:
        """解析 Markdown 内联样式，生成飞书 text_run 元素列表。

        支持：**粗体**、*斜体*、`行内代码`、~~删除线~~

        Args:
            text: 包含 Markdown 内联样式的文本

        Returns:
            飞书 text_run 元素列表
        """
        if not text:
            return [{"text_run": {"content": "", "text_element_style": {}}}]

        elements = []
        # 使用正则一次性匹配所有内联样式
        pattern = r'(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|~~(.+?)~~|[^*`~]+)'
        matches = re.findall(pattern, text)

        for match in matches:
            full, bold, italic, code, strike = match
            if bold:
                elements.append({
                    "text_run": {
                        "content": bold,
                        "text_element_style": {"bold": True}
                    }
                })
            elif italic:
                elements.append({
                    "text_run": {
                        "content": italic,
                        "text_element_style": {"italic": True}
                    }
                })
            elif code:
                elements.append({
                    "text_run": {
                        "content": code,
                        "text_element_style": {"inline_code": True}
                    }
                })
            elif strike:
                elements.append({
                    "text_run": {
                        "content": strike,
                        "text_element_style": {"strikethrough": True}
                    }
                })
            else:
                # 纯文本
                elements.append({
                    "text_run": {
                        "content": full,
                        "text_element_style": {}
                    }
                })

        if not elements:
            elements.append({
                "text_run": {"content": text, "text_element_style": {}}
            })

        return elements

    # -----------------------------------------------------------------------
    # 本地 Markdown 转换（不依赖飞书 API）
    # -----------------------------------------------------------------------

    # 代码块语言到飞书语言 ID 的映射
    CODE_LANGUAGE_MAP = {
        'python': 1, 'javascript': 2, 'java': 3, 'cpp': 4, 'c++': 4,
        'go': 5, 'rust': 6, 'sql': 7, 'typescript': 8, 'json': 9,
        'yaml': 10, 'xml': 11, 'css': 13, 'html': 14, 'php': 17,
        'swift': 18, 'kotlin': 19, 'bash': 20, 'shell': 20, 'sh': 20,
    }

    def markdown_to_blocks_local(self, content: str) -> List[Dict]:
        """本地将 Markdown 内容转换为飞书文档块结构，不依赖飞书 API。

        逐行解析 Markdown 语法，支持标题、列表、引用、代码块、
        任务列表、分割线等多种块类型。

        Args:
            content: Markdown 格式文本

        Returns:
            飞书文档块列表
        """
        if not content:
            return []

        lines = content.split('\n')
        blocks = []
        i = 0

        while i < len(lines):
            line = lines[i]

            # 空行跳过
            if not line.strip():
                i += 1
                continue

            # 代码块
            if line.strip().startswith('```'):
                lang_name = line.strip()[3:].strip().lower()
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('```'):
                    code_lines.append(lines[i])
                    i += 1
                # 跳过结束的 ```
                if i < len(lines):
                    i += 1
                code_text = '\n'.join(code_lines)
                lang_id = self.CODE_LANGUAGE_MAP.get(lang_name, 1)
                blocks.append({
                    "block_type": 14,
                    "code": {
                        "elements": self._parse_inline_elements(code_text),
                        "style": {"language": lang_id}
                    }
                })
                continue

            # 分割线（Divider 块无额外属性，仅需 block_type=18，
            # 不需要携带 divider:{} 字段，避免 API 1770001 错误）
            if line.strip() in ('---', '***', '___', '* * *'):
                blocks.append({"block_type": 18})
                i += 1
                continue

            # 标题
            heading_match = re.match(r'^(#{1,9})\s+(.+)$', line)
            if heading_match:
                level = len(heading_match.group(1))
                text = heading_match.group(2)
                block_type = level + 2  # # → block_type 3
                if block_type > 11:
                    block_type = 11
                heading_fields = {3: 'heading1', 4: 'heading2', 5: 'heading3',
                                  6: 'heading4', 7: 'heading5', 8: 'heading6',
                                  9: 'heading7', 10: 'heading8', 11: 'heading9'}
                blocks.append({
                    "block_type": block_type,
                    heading_fields[block_type]: {
                        "elements": self._parse_inline_elements(text)
                    }
                })
                i += 1
                continue

            # 无序列表
            ul_match = re.match(r'^[-*]\s+(.+)$', line)
            if ul_match:
                text = ul_match.group(1)
                blocks.append({
                    "block_type": 12,
                    "bullet": {
                        "elements": self._parse_inline_elements(text)
                    }
                })
                i += 1
                continue

            # 任务列表
            task_match = re.match(r'^-\s*\[([ xX])\]\s+(.+)$', line)
            if task_match:
                done = task_match.group(1).lower() == 'x'
                text = task_match.group(2)
                blocks.append({
                    "block_type": 17,
                    "todo": {
                        "elements": self._parse_inline_elements(text),
                        "style": {"done": done}
                    }
                })
                i += 1
                continue

            # 有序列表
            ol_match = re.match(r'^\d+\.\s+(.+)$', line)
            if ol_match:
                text = ol_match.group(1)
                blocks.append({
                    "block_type": 13,
                    "ordered": {
                        "elements": self._parse_inline_elements(text)
                    }
                })
                i += 1
                continue

            # 引用（支持多行连续引用合并）
            if line.startswith('> '):
                quote_lines = []
                while i < len(lines) and lines[i].startswith('> '):
                    quote_lines.append(lines[i][2:])
                    i += 1
                quote_text = '\n'.join(quote_lines)
                blocks.append({
                    "block_type": 15,
                    "quote": {
                        "elements": self._parse_inline_elements(quote_text)
                    }
                })
                continue

            # 普通文本
            blocks.append({
                "block_type": 2,
                "text": {
                    "elements": self._parse_inline_elements(line.strip())
                }
            })
            i += 1

        return blocks

    # -----------------------------------------------------------------------
    # 文档读取（核心方法，多级 Fallback）
    # -----------------------------------------------------------------------

    async def _extract_table_content_from_doc(self, doc_id: str) -> str:
        """从文档中提取表格、多维表格、电子表格的文本内容。

        飞书 docx raw_content API 不会返回表格内的文本，
        因此需要单独通过 blocks API 获取并提取。

        Args:
            doc_id: 文档 ID

        Returns:
            提取的表格文本内容，如果没有表格则返回空字符串
        """
        parts = []
        try:
            blocks = await self.get_document_blocks(doc_id)
        except Exception as e:
            logger.warning(f"获取文档块失败，无法补充表格: {e}")
            return ""

        for block in blocks:
            block_type = block.get('block_type', 0)
            block_id = block.get('block_id', '')

            if block_type == 20:  # 表格
                table = block.get('table', {})
                row_count = table.get('property', {}).get('row_size', 0)
                col_count = table.get('property', {}).get('column_size', 0)
                if row_count == 0 or not block_id:
                    continue

                try:
                    cells = await self.get_block_children(doc_id, block_id)
                except Exception as e:
                    logger.warning(f"获取表格子块失败 (block_id={block_id}): {e}")
                    parts.append(f"[表格: {row_count}行 x {col_count}列（无法读取内容）]")
                    continue

                # 按行组织单元格内容
                # 飞书表格单元格按先行后列的顺序排列
                row_texts = []
                current_row = []
                for cell in cells:
                    if cell.get('block_type') == 21:  # 表格单元格
                        cell_data = cell.get('table_cell', {})
                        elements = cell_data.get('elements', [])
                        cell_text = self._extract_text_from_elements(elements)
                        current_row.append(cell_text)

                # 如果单元格没有按行分组，尝试从 cell 结构推断
                if current_row:
                    # 单元格通常按顺序排列，每 col_count 个为一组
                    if col_count > 0 and len(current_row) >= col_count:
                        for r in range(0, len(current_row), col_count):
                            row_cells = current_row[r:r + col_count]
                            row_texts.append(" | ".join(row_cells))
                    else:
                        row_texts.append(" | ".join(current_row))

                if row_texts:
                    table_header = f"[表格: {row_count}行 x {col_count}列]"
                    parts.append(table_header + "\n" + "\n".join(row_texts))
                else:
                    parts.append(f"[表格: {row_count}行 x {col_count}列（空）]")

            elif block_type == 22:  # 多维表格 bitable
                bitable = block.get('bitable', {})
                token = bitable.get('token', '')
                if token:
                    try:
                        tables = await self.get_bitable_tables(token)
                        for t in tables[:10]:  # 最多读取10个数据表
                            table_name = t.get('name', t.get('table_id', ''))
                            table_id = t.get('table_id', '')
                            if table_id:
                                records = await self.get_bitable_records(token, table_id)
                                if records:
                                    parts.append(f"[多维表格: {table_name}]")
                                    for rec in records[:500]:
                                        fields = rec.get('fields', {})
                                        field_strs = [f"{k}: {v}" for k, v in fields.items()]
                                        parts.append(" | ".join(field_strs))
                                    if len(records) > 500:
                                        parts.append(f"...(共 {len(records)} 条记录)")
                    except Exception as e:
                        logger.warning(f"获取多维表格内容失败: {e}")
                        parts.append(f"[多维表格: {token}（无法读取内容）]")

            elif block_type == 23:  # 电子表格 sheet
                sheet = block.get('sheet', {})
                token = sheet.get('token', '')
                if token:
                    try:
                        sheets = await self.get_spreadsheet_sheets(token)
                        for s in sheets[:100]:
                            sheet_id = s.get('sheet_id', '')
                            sheet_title = s.get('title', sheet_id)
                            try:
                                rows = await self.read_spreadsheet_cells(
                                    token, sheet_id, 'A1:ZZ1000'
                                )
                                if rows:
                                    parts.append(f"[电子表格: {sheet_title}]")
                                    for row in rows[:1000]:
                                        parts.append(" | ".join(str(c) for c in row))
                                    if len(rows) > 1000:
                                        parts.append(f"...(共 {len(rows)} 行)")
                            except Exception as e2:
                                parts.append(f"[电子表格: {sheet_title}（无法读取: {e2}）]")
                    except Exception as e:
                        logger.warning(f"获取电子表格内容失败: {e}")
                        parts.append(f"[电子表格: {token}（无法读取内容）]")

        return "\n\n".join(parts) if parts else ""

    async def get_document_content(self, document_id: str) -> str:
        """获取文档内容，支持多种文档类型的多级 Fallback 读取。

        优先级：新版 docx raw_content API → 旧版 docs API → blocks API → 画板提取

        Args:
            document_id: 文档 ID 或 URL

        Returns:
            文档内容纯文本
        """
        doc_id = self._extract_document_id(document_id)
        if not doc_id:
            raise Exception("无法提取文档 ID，请提供有效的文档 ID 或 URL")

        # 判断是否为 Wiki 文档（仅当 URL 中明确含 /wiki/ 时才走 wiki 解析路径）
        is_wiki_url = '/wiki/' in document_id

        obj_token = doc_id
        obj_type = "docx"

        # 策略1：直接用 docx raw_content API（优先级最高，无论是否 wiki 都先尝试）
        try:
            result = await self._get(f"/docx/v1/documents/{doc_id}/raw_content")
            data = self._check_result(result, "获取文档内容(docx)")
            content = data.get('content', '')
            if content and len(content) > 10:
                logger.info(f"通过 docx raw_content API 获取内容成功，长度: {len(content)}")
                # raw_content API 不会返回表格内的文本内容（飞书 API 限制），
                # 需要额外通过 blocks API 补充表格/多维表格/电子表格内容
                try:
                    supplementary = await self._extract_table_content_from_doc(doc_id)
                    if supplementary:
                        content = content + "\n\n" + supplementary
                        logger.info(f"补充表格/多维表格/电子表格内容成功，补充长度: {len(supplementary)}")
                except Exception as e:
                    logger.warning(f"补充表格内容失败（不影响主流程）: {e}")
                return content
        except Exception as e:
            logger.warning(f"docx raw_content API 失败: {e}")

        # 策略2：如果是 wiki URL，尝试通过 get_node 解析后再读
        if is_wiki_url:
            try:
                result = await self._get(f"/wiki/v2/spaces/get_node", params={"token": doc_id})
                node_data = self._check_result(result, "获取 Wiki 节点")
                obj_token = node_data.get('obj_token', doc_id)
                obj_type = node_data.get('obj_type', 'docx')
                logger.info(f"Wiki 节点解析成功: obj_token={obj_token}, obj_type={obj_type}")
                # 用解析出的 obj_token 再次尝试 docx API
                if obj_token != doc_id:
                    try:
                        result2 = await self._get(f"/docx/v1/documents/{obj_token}/raw_content")
                        data2 = self._check_result(result2, "获取 Wiki 文档内容(docx)")
                        content2 = data2.get('content', '')
                        if content2 and len(content2) > 10:
                            logger.info(f"通过 Wiki→docx API 获取内容成功，长度: {len(content2)}")
                            # 同样补充表格内容
                            try:
                                supplementary = await self._extract_table_content_from_doc(obj_token)
                                if supplementary:
                                    content2 = content2 + "\n\n" + supplementary
                                    logger.info(f"补充表格内容成功，补充长度: {len(supplementary)}")
                            except Exception as e:
                                logger.warning(f"补充表格内容失败（不影响主流程）: {e}")
                            return content2
                    except Exception as e2:
                        logger.warning(f"Wiki→docx API 失败: {e2}")
            except Exception as e:
                logger.warning(f"获取 Wiki 节点信息失败: {e}")

        # 策略3：旧版 docs API（Wiki/旧版文档兼容，使用解析后的 obj_token）
        try:
            result = await self._get(
                f"/docs/v1/content",
                params={
                    "doc_token": obj_token,
                    "doc_type": obj_type,
                    "content_type": "markdown"
                }
            )
            data = self._check_result(result, "获取文档内容(docs)")
            content = data.get('content', '')
            if content and len(content) > 10:
                logger.info(f"通过 docs API 获取内容成功，长度: {len(content)}")
                return content
        except Exception as e:
            logger.warning(f"docs API 失败: {e}")

        # 策略4：blocks API + 画板提取（使用解析后的 obj_token）
        try:
            blocks = await self.get_document_blocks(obj_token)
            text_content = self.extract_text_from_blocks(blocks)

            # 在文档块中查找画板（block_type==43），获取画板内容
            board_texts = []
            # 在文档块中查找电子表格（block_type==23），获取表格内容
            sheet_texts = []
            # 在文档块中查找普通表格（block_type==20）和多维表格（block_type==22）
            table_texts = []
            for block in blocks:
                block_type = block.get('block_type', 0)
                block_id = block.get('block_id', '')

                if block_type == 20:  # 普通表格
                    table = block.get('table', {})
                    row_count = table.get('property', {}).get('row_size', 0)
                    col_count = table.get('property', {}).get('column_size', 0)
                    if row_count > 0 and block_id:
                        try:
                            cells = await self.get_block_children(obj_token, block_id)
                            cell_texts = []
                            for cell in cells:
                                if cell.get('block_type') == 21:
                                    cell_data = cell.get('table_cell', {})
                                    elements = cell_data.get('elements', [])
                                    cell_text = self._extract_text_from_elements(elements)
                                    cell_texts.append(cell_text)
                            if cell_texts:
                                table_texts.append(f"[表格: {row_count}行 x {col_count}列]")
                                if col_count > 0 and len(cell_texts) >= col_count:
                                    for r in range(0, len(cell_texts), col_count):
                                        row_cells = cell_texts[r:r + col_count]
                                        table_texts.append(" | ".join(row_cells))
                                else:
                                    table_texts.append(" | ".join(cell_texts))
                        except Exception as e:
                            logger.warning(f"获取表格内容失败 (block_id={block_id}): {e}")
                            table_texts.append(f"[表格: {row_count}行 x {col_count}列（无法读取内容）]")

                elif block_type == 22:  # 多维表格 bitable
                    bitable = block.get('bitable', {})
                    token = bitable.get('token', '')
                    if token:
                        try:
                            tables = await self.get_bitable_tables(token)
                            for t in tables[:10]:
                                table_name = t.get('name', t.get('table_id', ''))
                                table_id = t.get('table_id', '')
                                if table_id:
                                    records = await self.get_bitable_records(token, table_id)
                                    if records:
                                        table_texts.append(f"[多维表格: {table_name}]")
                                        for rec in records[:500]:
                                            fields = rec.get('fields', {})
                                            field_strs = [f"{k}: {v}" for k, v in fields.items()]
                                            table_texts.append(" | ".join(field_strs))
                                        if len(records) > 500:
                                            table_texts.append(f"...(共 {len(records)} 条记录)")
                        except Exception as e:
                            logger.warning(f"获取多维表格内容失败: {e}")
                            table_texts.append(f"[多维表格: {token}（无法读取内容）]")

                if block_type == 43:  # 画板
                    board_data = block.get('board', {})
                    whiteboard_id = board_data.get('token', '')
                    if whiteboard_id:
                        try:
                            nodes = await self.get_board_nodes(whiteboard_id)
                            logger.info(f"获取画板节点 {whiteboard_id}，共 {len(nodes)} 个节点")
                            for node in nodes[:30]:
                                # 画板文本节点结构: text.elements[].text_run.content
                                text_data = node.get('text', {})
                                elements = text_data.get('elements', [])
                                node_text = self._extract_text_from_elements(elements)
                                if node_text:
                                    board_texts.append(node_text)
                                # 也尝试直接 content 字段（兼容不同节点类型）
                                direct_content = node.get('content', '')
                                if direct_content and direct_content not in board_texts:
                                    board_texts.append(direct_content)
                        except Exception as e:
                            logger.warning(f"获取画板内容失败: {e}")

                elif block_type == 23:  # 电子表格
                    sheet_data = block.get('sheet', {})
                    spreadsheet_token = sheet_data.get('token', '')
                    if spreadsheet_token:
                        try:
                            sheets = await self.get_spreadsheet_sheets(spreadsheet_token)
                            logger.info(f"获取电子表格 {spreadsheet_token}，共 {len(sheets)} 个 Sheet")
                            for s in sheets[:100]:  # 最多读 100 个 sheet
                                sheet_id = s.get('sheet_id', '')
                                sheet_title = s.get('title', sheet_id)
                                try:
                                    rows = await self.read_spreadsheet_cells(
                                        spreadsheet_token, sheet_id, 'A1:ZZ1000'
                                    )
                                    if rows:
                                        sheet_texts.append(f"[Sheet: {sheet_title}]")
                                        for row in rows[:1000]:
                                            sheet_texts.append(" | ".join(str(c) for c in row))
                                        if len(rows) > 1000:
                                            sheet_texts.append(f"...(共 {len(rows)} 行)")
                                except Exception as e2:
                                    sheet_texts.append(f"[Sheet: {sheet_title} 读取失败: {e2}]")
                        except Exception as e:
                            logger.warning(f"获取电子表格内容失败: {e}")

            if board_texts:
                text_content += "\n\n[画板内容]\n" + "\n".join(board_texts)
            if sheet_texts:
                text_content += "\n\n[电子表格内容]\n" + "\n".join(sheet_texts)
            if table_texts:
                text_content += "\n\n[表格内容]\n" + "\n".join(table_texts)

            if text_content.strip():
                logger.info(f"通过 blocks API 获取内容成功，长度: {len(text_content)}")
                return text_content
            else:
                raise Exception("文档内容为空")
        except Exception as e:
            logger.error(f"blocks API 失败: {e}")

        # 所有策略都失败
        raise Exception(
            f"无法读取文档内容（ID: {doc_id}）。"
            f"请检查：1) 文档 ID 是否正确 2) 确认已授予 docx:document 权限 "
            f"3) 文档是否对应用可见 4) 应用是否已安装到文档所在空间"
        )

    # -----------------------------------------------------------------------
    # 文档创建
    # -----------------------------------------------------------------------

    async def create_document(self, title: str) -> Dict[str, Any]:
        """创建新的飞书文档。

        Args:
            title: 文档标题

        Returns:
            包含 document_id 等信息的字典
        """
        result = await self._post("/docx/v1/documents", data={"title": title})
        data = self._check_result(result, "创建文档")
        logger.info(f"文档创建成功: {data.get('document', {}).get('title', '')} "
                     f"({data.get('document', {}).get('document_id', '')})")
        return data

    # -----------------------------------------------------------------------
    # 文档搜索
    # -----------------------------------------------------------------------

    async def search_documents(self, query: str) -> List[Dict]:
        """搜索飞书文档（需要 drive:drive.search:readonly 权限）。

        Args:
            query: 搜索关键词

        Returns:
            匹配的文档列表
        """
        result = await self._post(
            "/search/v2/doc",
            data={"query": query, "type": "doc", "count": 20, "offset": 0}
        )
        data = self._check_result(result, "搜索文档")
        items = data.get('items', [])
        logger.info(f"搜索文档 '{query}' 返回 {len(items)} 条结果")
        return items

    # -----------------------------------------------------------------------
    # 文档信息
    # -----------------------------------------------------------------------

    async def get_document_info(self, document_id: str) -> Dict[str, Any]:
        """获取文档基本信息。

        Args:
            document_id: 文档 ID

        Returns:
            文档信息字典
        """
        doc_id = self._extract_document_id(document_id)
        result = await self._get(f"/docx/v1/documents/{doc_id}")
        data = self._check_result(result, "获取文档信息")
        return data

    # -----------------------------------------------------------------------
    # 文档块操作
    # -----------------------------------------------------------------------

    async def get_document_blocks(self, document_id: str, page_size: int = 500) -> List[Dict]:
        """分页获取所有文档块。

        Args:
            document_id: 文档 ID
            page_size: 每页数量

        Returns:
            文档块列表
        """
        doc_id = self._extract_document_id(document_id)
        all_blocks = []
        page_token = None

        while True:
            params = {
                "page_size": page_size,
                "document_revision_id": -1
            }
            if page_token:
                params["page_token"] = page_token

            result = await self._get(
                f"/docx/v1/documents/{doc_id}/blocks", params=params
            )
            data = self._check_result(result, "获取文档块")

            items = data.get('items', [])
            all_blocks.extend(items)

            has_more = data.get('has_more', False)
            page_token = data.get('page_token', '')

            if not has_more:
                break

        logger.info(f"获取文档块完成，共 {len(all_blocks)} 个块")
        return all_blocks

    async def get_block_children(self, document_id: str, block_id: str,
                                  page_size: int = 500) -> List[Dict]:
        """获取指定块的子块列表。

        Args:
            document_id: 文档 ID
            block_id: 父块 ID
            page_size: 每页数量

        Returns:
            子块列表
        """
        doc_id = self._extract_document_id(document_id)
        result = await self._get(
            f"/docx/v1/documents/{doc_id}/blocks/{block_id}/children",
            params={"page_size": page_size, "document_revision_id": -1}
        )
        data = self._check_result(result, "获取子块")
        return data.get('items', [])

    async def create_document_blocks(self, document_id: str, parent_block_id: str,
                                      blocks: List[Dict], index: int = 0) -> Dict:
        """在文档中创建新块。

        Args:
            document_id: 文档 ID
            parent_block_id: 父块 ID（可用文档 root block ID）
            blocks: 要创建的块列表
            index: 插入位置索引

        Returns:
            创建结果
        """
        doc_id = self._extract_document_id(document_id)
        result = await self._post(
            f"/docx/v1/documents/{doc_id}/blocks/{parent_block_id}/children",
            data={"index": index, "children": blocks},
            params={"document_revision_id": -1}
        )
        data = self._check_result(result, "创建文档块")
        logger.info(f"文档块创建成功，共 {len(blocks)} 个块")
        return data

    async def update_document_block(self, document_id: str, block_id: str,
                                     update_data: Dict) -> Dict:
        """更新文档块内容。

        Args:
            document_id: 文档 ID
            block_id: 要更新的块 ID
            update_data: 更新数据（如新的 text elements）

        Returns:
            更新结果
        """
        doc_id = self._extract_document_id(document_id)
        result = await self._patch(
            f"/docx/v1/documents/{doc_id}/blocks/{block_id}",
            data=update_data,
            params={"document_revision_id": -1}
        )
        data = self._check_result(result, "更新文档块")
        return data

    async def delete_document_block(self, document_id: str, block_id: str) -> Dict:
        """删除文档块。

        Args:
            document_id: 文档 ID
            block_id: 要删除的块 ID

        Returns:
            删除结果
        """
        doc_id = self._extract_document_id(document_id)
        result = await self._delete(
            f"/docx/v1/documents/{doc_id}/blocks/{block_id}",
            params={"document_revision_id": -1}
        )
        data = self._check_result(result, "删除文档块")
        return data

    # -----------------------------------------------------------------------
    # Markdown 转换（调用飞书 API）
    # -----------------------------------------------------------------------

    async def convert_markdown_to_blocks(self, content: str,
                                          content_type: str = "markdown") -> List[Dict]:
        """通过飞书 API 将 Markdown 转换为文档块（需要 docx:document.block:convert 权限）。

        Args:
            content: Markdown 内容
            content_type: 内容类型，默认 markdown

        Returns:
            转换后的文档块列表
        """
        result = await self._post(
            "/docx/v1/documents/blocks/convert",
            data={"content": content, "type": content_type}
        )

        code = result.get('code')
        if code == 0:
            data = result.get('data', {})
            return data.get('children', [])
        elif code == 99992402:
            raise Exception(
                "权限不足：请在飞书开放平台添加 'docx:document.block:convert' 权限"
            )
        else:
            raise Exception(f"Markdown转换失败: {result.get('msg', '未知错误')} (code: {code})")

    # -----------------------------------------------------------------------
    # 画板 API
    # -----------------------------------------------------------------------

    async def get_board_nodes(self, whiteboard_id: str) -> List[Dict]:
        """获取画板节点列表。

        Args:
            whiteboard_id: 画板 ID

        Returns:
            画板节点列表
        """
        result = await self._get(f"/board/v1/whiteboards/{whiteboard_id}/nodes")
        data = self._check_result(result, "获取画板节点")
        return data.get('nodes', [])

    async def create_board_node(self, whiteboard_id: str, node_type: str = "text",
                                 content: str = "", position: Dict = None) -> Dict:
        """在画板中创建节点。

        Args:
            whiteboard_id: 画板 ID
            node_type: 节点类型，默认 text
            content: 节点内容
            position: 节点位置坐标

        Returns:
            创建结果
        """
        payload = {"type": node_type, "content": content}
        if position:
            payload["position"] = position
        result = await self._post(
            f"/board/v1/whiteboards/{whiteboard_id}/nodes", data=payload
        )
        data = self._check_result(result, "创建画板节点")
        return data

    async def delete_board_nodes(self, whiteboard_id: str, node_ids: List[str]) -> Dict:
        """批量删除画板节点。

        Args:
            whiteboard_id: 画板 ID
            node_ids: 要删除的节点 ID 列表

        Returns:
            删除结果
        """
        result = await self._post(
            f"/board/v1/whiteboards/{whiteboard_id}/nodes/batch_delete",
            data={"node_ids": node_ids}
        )
        data = self._check_result(result, "删除画板节点")
        return data

    # -----------------------------------------------------------------------
    # 知识库（Wiki）API
    # -----------------------------------------------------------------------

    async def get_wiki_spaces(self) -> List[Dict]:
        """获取所有知识库空间列表（支持分页，最多500个）。

        Returns:
            知识库列表
        """
        all_spaces = []
        page_token = None
        while True:
            params: Dict[str, Any] = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            result = await self._get("/wiki/v2/spaces", params=params)
            data = self._check_result(result, "获取知识库列表")
            items = data.get('items', [])
            all_spaces.extend(items)
            if not data.get('has_more', False):
                break
            page_token = data.get('page_token', '')
            if len(all_spaces) >= 500:
                break
        logger.info(f"获取知识库列表完成，共 {len(all_spaces)} 个空间")
        return all_spaces

    async def get_wiki_nodes(self, space_id: str) -> List[Dict]:
        """获取知识库下的文档节点列表（支持分页，最多500个）。

        Args:
            space_id: 知识库空间 ID

        Returns:
            节点列表（每个节点包含 title, obj_token, obj_type, node_token, has_child 等）
        """
        all_nodes = []
        page_token = None
        while True:
            params: Dict[str, Any] = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            result = await self._get(
                f"/wiki/v2/spaces/{space_id}/nodes",
                params=params
            )
            data = self._check_result(result, "获取知识库节点")
            items = data.get('items', [])
            all_nodes.extend(items)
            if not data.get('has_more', False):
                break
            page_token = data.get('page_token', '')
            if len(all_nodes) >= 500:
                break
        logger.info(f"获取知识库节点完成，共 {len(all_nodes)} 个节点")
        return all_nodes

    async def create_wiki_node(self, space_id: str, title: str,
                                obj_type: str = "docx") -> Dict:
        """在知识库中创建文档节点。

        Args:
            space_id: 知识库空间 ID
            title: 文档标题
            obj_type: 对象类型，默认 docx

        Returns:
            创建结果
        """
        result = await self._post(
            f"/wiki/v2/spaces/{space_id}/nodes/create",
            data={"title": title, "obj_type": obj_type}
        )
        data = self._check_result(result, "创建知识库节点")
        return data

    async def move_wiki_node(self, space_id: str, node_id: str,
                              parent_node_id: str = None) -> Dict:
        """移动知识库节点。

        Args:
            space_id: 知识库空间 ID
            node_id: 要移动的节点 ID
            parent_node_id: 目标父节点 ID，为空则移到根目录

        Returns:
            移动结果
        """
        payload = {}
        if parent_node_id:
            payload["parent_node_token"] = parent_node_id
        result = await self._post(
            f"/wiki/v2/spaces/{space_id}/nodes/{node_id}/move", data=payload
        )
        data = self._check_result(result, "移动知识库节点")
        return data

    async def delete_wiki_node(self, space_id: str, node_id: str) -> Dict:
        """删除知识库节点。

        Args:
            space_id: 知识库空间 ID
            node_id: 要删除的节点 ID

        Returns:
            删除结果
        """
        result = await self._delete(
            f"/wiki/v2/spaces/{space_id}/nodes/{node_id}"
        )
        data = self._check_result(result, "删除知识库节点")
        return data

    async def get_wiki_members(self, space_id: str) -> List[Dict]:
        """获取知识库成员列表。

        Args:
            space_id: 知识库空间 ID

        Returns:
            成员列表
        """
        result = await self._get(
            f"/wiki/v2/spaces/{space_id}/members",
            params={"page_size": 50}
        )
        data = self._check_result(result, "获取知识库成员")
        return data.get('items', [])

    async def add_wiki_member(self, space_id: str, member_type: str,
                               member_id: str, perm: str = "view") -> Dict:
        """添加知识库成员。

        Args:
            space_id: 知识库空间 ID
            member_type: 成员类型（如 openid, user_id 等）
            member_id: 成员 ID
            perm: 权限（view, edit, manage），默认 view

        Returns:
            添加结果
        """
        result = await self._post(
            f"/wiki/v2/spaces/{space_id}/members",
            data={
                "member_type": member_type,
                "member_id": member_id,
                "perm": perm
            }
        )
        data = self._check_result(result, "添加知识库成员")
        return data

    # -----------------------------------------------------------------------
    # 权限管理 API
    # -----------------------------------------------------------------------

    async def add_document_permission(self, doc_token: str, doc_type: str,
                                       member_type: str, member_id: str,
                                       perm: str) -> Dict:
        """添加文档权限给指定成员。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型（docx, sheet, bitable 等）
            member_type: 成员类型
            member_id: 成员 ID
            perm: 权限（view, edit, full_access）

        Returns:
            操作结果
        """
        result = await self._post(
            f"/drive/v1/permissions/{doc_token}/members",
            data={
                "member_type": member_type,
                "member_id": member_id,
                "perm": perm
            },
            params={"type": doc_type}
        )
        data = self._check_result(result, "添加文档权限")
        return data

    async def transfer_document_owner(self, doc_token: str, doc_type: str,
                                       member_type: str, member_id: str,
                                       remove_old_owner: bool = False) -> Dict:
        """转移文档所有权。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型
            member_type: 新所有者成员类型
            member_id: 新所有者成员 ID
            remove_old_owner: 是否移除旧所有者权限

        Returns:
            操作结果
        """
        result = await self._post(
            f"/drive/v1/permissions/{doc_token}/members/transfer_owner",
            data={
                "member_type": member_type,
                "member_id": member_id
            },
            params={
                "type": doc_type,
                "remove_old_owner": str(remove_old_owner).lower()
            }
        )
        data = self._check_result(result, "转移文档所有权")
        return data

    async def get_document_collaborators(self, doc_token: str, doc_type: str) -> List[Dict]:
        """获取文档协作者列表。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型

        Returns:
            协作者列表
        """
        result = await self._get(
            f"/drive/v1/permissions/{doc_token}/members",
            params={"type": doc_type}
        )
        data = self._check_result(result, "获取协作者")
        return data.get('items', data.get('members', []))

    async def get_permission_settings(self, doc_token: str, doc_type: str) -> Dict:
        """获取文档权限设置。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型

        Returns:
            权限设置信息
        """
        result = await self._get(
            f"/drive/v1/permissions/{doc_token}/settings",
            params={"type": doc_type}
        )
        data = self._check_result(result, "获取权限设置")
        return data

    async def update_permission_settings(self, doc_token: str, doc_type: str,
                                          settings: Dict) -> Dict:
        """更新文档权限设置。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型
            settings: 新的权限设置

        Returns:
            更新结果
        """
        result = await self._patch(
            f"/drive/v1/permissions/{doc_token}/settings",
            data=settings,
            params={"type": doc_type}
        )
        data = self._check_result(result, "更新权限设置")
        return data

    # -----------------------------------------------------------------------
    # 多维表格（Bitable）API
    # -----------------------------------------------------------------------

    async def get_bitable_tables(self, app_token: str) -> List[Dict]:
        """获取多维表格的数据表列表。

        Args:
            app_token: 多维表格 app token

        Returns:
            数据表列表
        """
        result = await self._get(f"/bitable/v1/apps/{app_token}/tables")
        data = self._check_result(result, "获取多维表格表列表")
        return data.get('items', [])

    async def get_bitable_records(self, app_token: str, table_id: str,
                                   page_size: int = 100) -> List[Dict]:
        """获取多维表格记录。

        Args:
            app_token: 多维表格 app token
            table_id: 数据表 ID
            page_size: 每页数量

        Returns:
            记录列表
        """
        result = await self._get(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params={"page_size": page_size}
        )
        data = self._check_result(result, "获取多维表格记录")
        return data.get('items', [])

    async def create_bitable_record(self, app_token: str, table_id: str,
                                     fields: Dict) -> Dict:
        """在多维表格中创建记录。

        Args:
            app_token: 多维表格 app token
            table_id: 数据表 ID
            fields: 字段键值对

        Returns:
            创建结果
        """
        result = await self._post(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            data={"fields": fields}
        )
        data = self._check_result(result, "创建多维表格记录")
        return data

    async def update_bitable_record(self, app_token: str, table_id: str,
                                     record_id: str, fields: Dict) -> Dict:
        """更新多维表格记录。

        Args:
            app_token: 多维表格 app token
            table_id: 数据表 ID
            record_id: 记录 ID
            fields: 要更新的字段键值对

        Returns:
            更新结果
        """
        result = await self._put(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            data={"fields": fields}
        )
        data = self._check_result(result, "更新多维表格记录")
        return data

    async def _put(self, path: str, data: dict = None, params: dict = None) -> dict:
        """发送 PUT 请求。

        Args:
            path: API 路径
            data: JSON 请求体
            params: 查询参数

        Returns:
            API 响应
        """
        token = await self.get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=data, params=params) as resp:
                return await resp.json()

    async def delete_bitable_record(self, app_token: str, table_id: str,
                                     record_id: str) -> Dict:
        """删除多维表格记录。

        Args:
            app_token: 多维表格 app token
            table_id: 数据表 ID
            record_id: 记录 ID

        Returns:
            删除结果
        """
        result = await self._delete(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        )
        data = self._check_result(result, "删除多维表格记录")
        return data

    # -----------------------------------------------------------------------
    # 电子表格（Sheet）API
    # -----------------------------------------------------------------------

    async def get_spreadsheet_sheets(self, spreadsheet_token: str) -> List[Dict]:
        """获取电子表格的 sheet 列表。

        Args:
            spreadsheet_token: 电子表格 token

        Returns:
            sheet 列表
        """
        result = await self._get(
            f"/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
        )
        data = self._check_result(result, "获取电子表格sheet列表")
        return data.get('sheets', [])

    async def read_spreadsheet_cells(self, spreadsheet_token: str,
                                      sheet_id: str, range_: str = 'A1:ZZ1000') -> List[List]:
        """读取电子表格单元格区域的值。

        Args:
            spreadsheet_token: 电子表格 token
            sheet_id: sheet ID（如 "Sheet1"）
            range_: 单元格范围（如 "A1:C10"），默认读全表

        Returns:
            二维数组形式的单元格值
        """
        result = await self._get(
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!{range_}"
        )
        data = self._check_result(result, "读取电子表格单元格")
        return data.get('value_range', {}).get('values', [])

    @staticmethod
    def _parse_sheet_range(range_str: str):
        """解析组合格式的电子表格范围字符串。

        输入示例：
        - "Sheet1!A1:C10" → ("Sheet1", "A1:C10")
        - "Sheet1" → ("Sheet1", "A1:ZZ1000")
        - "A1:C10" → ("Sheet1", "A1:C10")  # 默认 sheet

        Returns:
            (sheet_id, range_) 元组
        """
        if not range_str:
            return ("Sheet1", "A1:ZZ1000")
        if '!' in range_str:
            parts = range_str.split('!', 1)
            return (parts[0], parts[1] if len(parts) > 1 else 'A1:ZZ1000')
        # 不含 ! — 如果看起来像范围（含 :）则是纯范围，否则是 sheet 名
        if ':' in range_str or range_str[0].isalpha() and range_str[1:].isdigit() if len(range_str) > 1 else False:
            # 可能是范围如 "A1:C10" 或单元格如 "A1"
            return ("Sheet1", range_str)
        return (range_str, "A1:ZZ1000")

    async def write_spreadsheet_cells(self, spreadsheet_token: str, sheet_id: str,
                                       range_: str, values: List[List]) -> Dict:
        """写入电子表格单元格区域。

        Args:
            spreadsheet_token: 电子表格 token
            sheet_id: sheet ID
            range_: 单元格范围
            values: 二维数组形式的值

        Returns:
            写入结果
        """
        result = await self._put(
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            data={
                "value_range": {
                    "range": f"{sheet_id}!{range_}",
                    "values": values
                }
            }
        )
        data = self._check_result(result, "写入电子表格单元格")
        return data

    async def create_spreadsheet(self, title: str, folder_token: str = None) -> Dict:
        """创建电子表格。

        Args:
            title: 标题
            folder_token: 目标文件夹 token，可选

        Returns:
            创建结果
        """
        payload: Dict[str, Any] = {"title": title}
        if folder_token:
            payload["folder_token"] = folder_token
        result = await self._post("/sheets/v3/spreadsheets", data=payload)
        data = self._check_result(result, "创建电子表格")
        return data

    # -----------------------------------------------------------------------
    # 幻灯片 API
    # -----------------------------------------------------------------------

    async def create_presentation(self, title: str, folder_token: str = None) -> Dict:
        """创建幻灯片演示文稿。

        Args:
            title: 标题
            folder_token: 目标文件夹 token，可选

        Returns:
            创建结果
        """
        payload: Dict[str, Any] = {"title": title}
        if folder_token:
            payload["folder_token"] = folder_token
        result = await self._post("/slides/v1/presentations", data=payload)
        data = self._check_result(result, "创建幻灯片")
        return data

    async def get_presentation_info(self, presentation_id: str) -> Dict:
        """获取幻灯片信息。

        Args:
            presentation_id: 幻灯片 ID

        Returns:
            幻灯片信息
        """
        result = await self._get(f"/slides/v1/presentations/{presentation_id}")
        data = self._check_result(result, "获取幻灯片信息")
        return data

    # -----------------------------------------------------------------------
    # 云文档评论 API
    # -----------------------------------------------------------------------

    async def get_document_comments(self, doc_token: str,
                                     doc_type: str = "docx") -> List[Dict]:
        """获取文档评论列表。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型

        Returns:
            评论列表
        """
        result = await self._get(
            f"/drive/v1/files/{doc_token}/comments",
            params={"file_type": doc_type}
        )
        data = self._check_result(result, "获取文档评论")
        return data.get('items', [])

    async def add_document_comment(self, doc_token: str, doc_type: str,
                                    content: str, parent_id: str = None) -> Dict:
        """添加文档评论。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型
            content: 评论内容
            parent_id: 父评论 ID（用于回复），可选

        Returns:
            添加结果
        """
        payload: Dict[str, Any] = {"content": content}
        if parent_id:
            payload["reply_list"] = {"reply_to": parent_id}
        result = await self._post(
            f"/drive/v1/files/{doc_token}/comments",
            data=payload,
            params={"file_type": doc_type}
        )
        data = self._check_result(result, "添加文档评论")
        return data

    # -----------------------------------------------------------------------
    # 云文档导出/复制 API
    # -----------------------------------------------------------------------

    async def copy_document(self, file_token: str, file_type: str,
                             name: str = None, folder_token: str = None) -> Dict:
        """复制文档。

        Args:
            file_token: 文件 token
            file_type: 文件类型
            name: 新文件名，可选
            folder_token: 目标文件夹 token，可选

        Returns:
            复制结果
        """
        payload: Dict[str, Any] = {}
        if name:
            payload["name"] = name
        if folder_token:
            payload["folder_token"] = folder_token
        result = await self._post(
            f"/drive/v1/files/{file_token}/copy",
            data=payload,
            params={"type": file_type}
        )
        data = self._check_result(result, "复制文档")
        return data

    # 飞书导出 API 支持的格式映射：{file_type: [支持的 file_extension]}
    EXPORT_FORMAT_MAP = {
        "docx": ["pdf", "docx"],
        "doc": ["pdf", "docx"],
        "sheet": ["pdf", "xlsx", "csv"],
        "bitable": ["pdf", "xlsx", "csv"],
    }

    async def export_document(self, file_token: str, file_type: str,
                               export_type: str = "pdf") -> Dict:
        """导出文档。

        Args:
            file_token: 文件 token
            file_type: 文件类型（docx/doc/sheet/bitable）
            export_type: 导出格式（pdf/docx/xlsx/csv，取决于 file_type）

        Returns:
            导出任务信息

        Raises:
            Exception: 格式不兼容时抛出明确错误
        """
        # 校验导出格式兼容性
        supported = self.EXPORT_FORMAT_MAP.get(file_type, [])
        if export_type not in supported:
            supported_str = "、".join(supported)
            raise Exception(
                f"不支持将 {file_type} 导出为 {export_type} 格式。"
                f"{file_type} 类型仅支持导出为: {supported_str}"
            )

        result = await self._post(
            "/drive/v1/export_tasks",
            data={
                "file_extension": export_type,
                "token": file_token,
                "type": file_type
            }
        )
        data = self._check_result(result, "导出文档")
        return data

    # -----------------------------------------------------------------------
    # 云空间文件管理 API
    # -----------------------------------------------------------------------

    async def get_folder_files(self, folder_token: str = None,
                                page_size: int = 50) -> List[Dict]:
        """获取文件夹下的文件列表。

        Args:
            folder_token: 文件夹 token，为空则列出根目录
            page_size: 每页数量

        Returns:
            文件列表
        """
        params: Dict[str, Any] = {"page_size": page_size}
        if folder_token:
            params["folder_token"] = folder_token
        result = await self._get("/drive/v1/files", params=params)
        data = self._check_result(result, "获取文件列表")
        return data.get('files', [])

    async def move_file(self, file_token: str, file_type: str,
                         folder_token: str) -> Dict:
        """移动文件到指定文件夹。

        Args:
            file_token: 文件 token
            file_type: 文件类型
            folder_token: 目标文件夹 token

        Returns:
            移动结果
        """
        result = await self._post(
            f"/drive/v1/files/{file_token}/move",
            data={"folder_token": folder_token},
            params={"file_type": file_type}
        )
        data = self._check_result(result, "移动文件")
        return data

    async def delete_file(self, file_token: str, file_type: str) -> Dict:
        """删除文件。

        Args:
            file_token: 文件 token
            file_type: 文件类型

        Returns:
            删除结果
        """
        result = await self._delete(
            f"/drive/v1/files/{file_token}",
            params={"type": file_type}
        )
        data = self._check_result(result, "删除文件")
        return data

    async def create_folder(self, folder_name: str,
                             parent_token: str = None) -> Dict:
        """创建文件夹。

        Args:
            folder_name: 文件夹名称
            parent_token: 父文件夹 token，为空则在根目录创建

        Returns:
            创建结果
        """
        payload: Dict[str, Any] = {"name": folder_name}
        if parent_token:
            payload["parent_token"] = parent_token
        else:
            payload["parent_type"] = "ccm"
        result = await self._post("/drive/v1/files/create_folder", data=payload)
        data = self._check_result(result, "创建文件夹")
        return data

    async def create_shortcut(self, file_token: str, file_type: str,
                               folder_token: str = None, name: str = None) -> Dict:
        """创建文件快捷方式。

        Args:
            file_token: 文件 token
            file_type: 文件类型
            folder_token: 目标文件夹 token，可选
            name: 快捷方式名称，可选

        Returns:
            创建结果
        """
        payload: Dict[str, Any] = {}
        if folder_token:
            payload["folder_token"] = folder_token
        if name:
            payload["name"] = name
        result = await self._post(
            "/drive/v1/files/create_shortcut",
            data=payload,
            params={"file_type": file_type, "file_token": file_token}
        )
        data = self._check_result(result, "创建快捷方式")
        return data

    # -----------------------------------------------------------------------
    # 文件上传/下载 API
    # -----------------------------------------------------------------------

    async def upload_file(self, file_name: str, file_content: bytes,
                           folder_token: str = None) -> Dict:
        """上传文件到飞书云空间。

        Args:
            file_name: 文件名
            file_content: 文件内容（bytes）
            folder_token: 目标文件夹 token，可选

        Returns:
            上传结果
        """
        token = await self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        form = aiohttp.FormData()
        form.add_field('file_name', file_name)
        form.add_field('file', file_content, filename=file_name,
                       content_type='application/octet-stream')
        form.add_field('parent_type', 'explorer')
        if folder_token:
            form.add_field('parent_node', folder_token)

        url = f"{self.base_url}/drive/v1/files/upload_all"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=form) as resp:
                result = await resp.json()

        data = self._check_result(result, "上传文件")
        return data

    async def download_file(self, file_token: str) -> bytes:
        """下载文件。

        Args:
            file_token: 文件 token

        Returns:
            文件二进制内容
        """
        token = await self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.base_url}/drive/v1/files/{file_token}/download"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.read()
                else:
                    result = await resp.json()
                    raise Exception(f"下载文件失败: {result.get('msg', '未知错误')}")

    # -----------------------------------------------------------------------
    # 文档版本管理 API
    # -----------------------------------------------------------------------

    async def get_document_versions(self, doc_token: str, doc_type: str) -> List[Dict]:
        """获取文档版本列表。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型

        Returns:
            版本列表
        """
        result = await self._get(
            f"/drive/v1/files/{doc_token}/versions",
            params={"type": doc_type}
        )
        data = self._check_result(result, "获取文档版本")
        return data.get('items', [])

    async def create_document_version(self, doc_token: str, doc_type: str,
                                       version_name: str = None) -> Dict:
        """创建文档版本快照。

        Args:
            doc_token: 文档 token
            doc_type: 文档类型
            version_name: 版本名称，可选

        Returns:
            创建结果
        """
        payload = {}
        if version_name:
            payload["name"] = version_name
        result = await self._post(
            f"/drive/v1/files/{doc_token}/versions",
            data=payload,
            params={"type": doc_type}
        )
        data = self._check_result(result, "创建文档版本")
        return data

    # -----------------------------------------------------------------------
    # 文档编辑/删除 API（编辑文档标题、追加内容、删除文档）
    # -----------------------------------------------------------------------

    async def update_document_title(self, document_id: str, new_title: str) -> Dict:
        """更新文档标题（通过飞书云盘 API 重命名文件）。

        注意：飞书文档新版 docx API 的 PATCH /docx/v1/documents 接口不支持直接修改
        title 字段（仅支持 update_display_setting 和 update_cover），因此通过云盘
        文件重命名接口 PATCH /drive/v1/files 实现标题更新。

        Args:
            document_id: 文档 ID 或 URL
            new_title: 新标题

        Returns:
            更新结果
        """
        doc_id = self._extract_document_id(document_id)
        result = await self._patch(
            f"/drive/v1/files/{doc_id}",
            data={"name": new_title}
        )
        data = self._check_result(result, "更新文档标题")
        return data

    async def append_document_content(self, document_id: str, content: str) -> Dict:
        """向文档末尾追加 Markdown 内容。

        Args:
            document_id: 文档 ID 或 URL
            content: Markdown 格式内容

        Returns:
            创建结果
        """
        doc_id = self._extract_document_id(document_id)

        # 获取文档根 block ID（即 page block ID = document_id）
        info = await self.get_document_info(doc_id)
        doc_data = info.get('document', info)
        root_block_id = doc_data.get('document_id', doc_id)

        # index=-1 表示追加到页面块子元素末尾（官方 SDK 示例使用 -1）
        insert_index = -1

        # 将 Markdown 转换为飞书 blocks
        new_blocks = self.markdown_to_blocks_local(content)

        if not new_blocks:
            return {"message": "没有可追加的内容"}

        # 在文档末尾追加 blocks
        result = await self.create_document_blocks(
            doc_id, root_block_id, new_blocks, index=insert_index
        )
        return result

    async def delete_document(self, document_id: str) -> Dict:
        """将文档移入回收站。

        Args:
            document_id: 文档 ID 或 URL

        Returns:
            删除结果
        """
        doc_id = self._extract_document_id(document_id)
        result = await self._delete(
            f"/drive/v1/files/{doc_id}",
            params={"type": "docx"}
        )
        data = self._check_result(result, "删除文档")
        return data

    # -----------------------------------------------------------------------
    # 电子表格追加数据 API
    # -----------------------------------------------------------------------

    async def append_values(self, spreadsheet_token: str, range_: str, values: List[List]) -> Dict:
        """向电子表格末尾追加行数据。

        Args:
            spreadsheet_token: 电子表格 token
            range_: 参考范围，如 Sheet1!A1:B1
            values: 二维数组形式的要追加的数据

        Returns:
            追加结果
        """
        result = await self._post(
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values_append",
            data={
                "value_range": {
                    "range": range_,
                    "values": values
                }
            }
        )
        data = self._check_result(result, "追加电子表格数据")
        return data

    # -----------------------------------------------------------------------
    # 云空间根目录元数据 API
    # -----------------------------------------------------------------------

    async def get_root_folder_meta(self) -> Dict:
        """获取云空间根目录元数据。

        Returns:
            根目录信息，包含 token
        """
        result = await self._get(
            "/drive/v1/files",
            params={"page_size": 1, "parent_type": "ccm"}
        )
        data = self._check_result(result, "获取根目录元数据")
        return data.get('files', [{}])[0] if data.get('files') else {}


# ============================================================================
# FunctionTool 子类 — FindAndSummarizeWikiTool
# ============================================================================

@pydantic_dataclass
class FindAndSummarizeWikiTool(FunctionTool[AstrAgentContext]):
    """根据知识库名称查找并总结知识库内容（一步完成）。

    该工具会：
    1. 搜索匹配的知识库
    2. 遍历其中文档（最多 10 个）
    3. 汇总内容返回给 AI 进行二次处理
    """

    name: str = "find_and_summarize_wiki_v2"
    description: str = (
        "根据知识库名称查找并总结知识库内容（一步完成）。"
        "当你需要了解某个知识库的内容时可以调用此工具。"
        "只需提供知识库名称，工具会自动查找并汇总其中所有文档的内容。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "wiki_name": {
                "type": "string",
                "description": "知识库名称（支持模糊匹配，不区分大小写）"
            }
        },
        "required": ["wiki_name"]
    })

    async def call(self, context: AstrAgentContext, **kwargs) -> str:
        """执行知识库查找与汇总。

        Args:
            context: AstrAgent 上下文
            **kwargs: 包含 wiki_name（知识库名称）

        Returns:
            汇总知识库内容文本
        """
        wiki_name = kwargs.get('wiki_name', '')
        if not wiki_name:
            return "❌ 请提供知识库名称"

        client = self.plugin.feishu_client
        if not client:
            return "❌ 飞书客户端未配置，请先设置 App ID 和 App Secret"

        try:
            # 获取所有知识库
            spaces = await client.get_wiki_spaces()
            if not spaces:
                return "未找到任何知识库"

            # 模糊匹配知识库名称
            matched_space = None
            for space in spaces:
                space_name = space.get('name', '')
                if wiki_name.lower() in space_name.lower():
                    matched_space = space
                    break

            if not matched_space:
                space_names = [s.get('name', '') for s in spaces[:10]]
                return (f"未找到匹配 '{wiki_name}' 的知识库。"
                        f"可用的知识库: {', '.join(space_names)}")

            space_id = matched_space.get('space_id', '')
            space_name = matched_space.get('name', '')

            # 获取节点列表
            nodes = await client.get_wiki_nodes(space_id)
            if not nodes:
                return f"知识库 '{space_name}' 中没有文档节点"

            # 遍历文档并汇总内容
            results = [f"📚 知识库: {space_name} (space_id: {space_id}, 共 {len(nodes)} 个节点)\n"]
            count = 0

            for i, node in enumerate(nodes[:10], 1):
                node_title = node.get('title', '无标题')
                obj_token = node.get('obj_token', '')
                obj_type = node.get('obj_type', 'docx')
                node_token = node.get('node_token', '')
                wiki_url = f"https://my.feishu.cn/wiki/{node_token}" if node_token else (
                    f"https://bytedance.feishu.cn/docx/{obj_token}" if obj_token else "无链接"
                )

                if not obj_token:
                    results.append(f"\n【{i}】📄 {node_title} [{obj_type}]: 无法获取内容（缺少 token）\n    链接: {wiki_url}")
                    continue

                try:
                    content = await client.get_document_content(obj_token)
                    # 截断过长内容
                    if len(content) > 2000:
                        content = content[:2000] + "\n...(内容已截断)"
                    results.append(
                        f"\n【{i}】📄 {node_title} [{obj_type}]\n"
                        f"    链接: {wiki_url}\n"
                        f"    obj_token: {obj_token}\n"
                        f"{content}"
                    )
                    count += 1
                except Exception as e:
                    results.append(
                        f"\n【{i}】📄 {node_title} [{obj_type}]\n"
                        f"    链接: {wiki_url}\n"
                        f"    读取失败: {e}"
                    )

            results.append(
                f"\n\n---\n以上是知识库 '{space_name}' 的内容汇总。"
                '每个文档标注了序号【N】，用户说「第N个模板/文档」时请对应序号。'
                "请根据用户的请求对以上内容进行分析、总结或回答。"
            )
            return "\n".join(results)

        except Exception as e:
            logger.error(f"FindAndSummarizeWikiTool 执行失败: {e}")
            return f"❌ 操作失败: {e}"


# ============================================================================
# FunctionTool 子类 — FindAndReadDocTool
# ============================================================================

@pydantic_dataclass
class FindAndReadDocTool(FunctionTool[AstrAgentContext]):
    """根据关键词搜索并读取第一个匹配的文档内容（一步完成）。

    该工具会：
    1. 用关键词搜索文档
    2. 读取第一个匹配文档的完整内容
    3. 返回内容供 AI 二次处理
    """

    name: str = "find_and_read_doc_v2"
    description: str = (
        "根据关键词搜索并读取第一个匹配的文档内容（一步完成）。"
        "当你需要查找并阅读某个主题的飞书文档时可以调用此工具。"
        "只需提供搜索关键词，工具会自动搜索并返回第一个匹配文档的内容。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "搜索关键词"
            }
        },
        "required": ["keyword"]
    })

    async def call(self, context: AstrAgentContext, **kwargs) -> str:
        """执行搜索并读取文档。

        Args:
            context: AstrAgent 上下文
            **kwargs: 包含 keyword（搜索关键词）

        Returns:
            文档内容文本
        """
        keyword = kwargs.get('keyword', '')
        if not keyword:
            return "❌ 请提供搜索关键词"

        client = self.plugin.feishu_client
        if not client:
            return "❌ 飞书客户端未配置，请先设置 App ID 和 App Secret"

        try:
            # 搜索文档
            items = await client.search_documents(keyword)
            if not items:
                return f"未找到与 '{keyword}' 相关的文档"

            # 读取第一个匹配文档
            first_doc = items[0]
            doc_title = first_doc.get('title', '无标题')
            doc_id = first_doc.get('doc_id', first_doc.get('token', ''))

            if not doc_id:
                return f"找到文档 '{doc_title}'，但无法获取文档 ID"

            content = await client.get_document_content(doc_id)

            # 截断过长内容
            if len(content) > 10000:
                content = content[:10000] + "\n...(内容已截断)"

            # 生成正确格式的链接
            if doc_id.startswith('doxcn'):
                doc_url = f"https://bytedance.feishu.cn/docx/{doc_id}"
            else:
                doc_url = f"https://my.feishu.cn/wiki/{doc_id}"

            return (
                f"📄 文档标题: {doc_title}\n"
                f"📎 文档 ID: {doc_id}\n"
                f"📎 链接: {doc_url}\n\n"
                f"{content}\n\n"
                f"---\n以上是文档 '{doc_title}' 的原始内容。"
                "请根据用户的请求对内容进行二次处理后再回复用户。"
            )

        except Exception as e:
            logger.error(f"FindAndReadDocTool 执行失败: {e}")
            return f"❌ 操作失败: {e}"


# ============================================================================
# Main 类 — AstrBot 插件主类
# ============================================================================

class Main(Star):
    """飞书文档控制器 AstrBot 插件主类。

    提供文档搜索、读取、创建等核心功能，
    支持 AI 自动调用（通过 @filter.llm_tool 和 FunctionTool 机制）。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化插件。

        Args:
            context: AstrBot 上下文
            config: 插件配置
        """
        super().__init__(context)

        # 打印配置信息
        logger.info("=" * 50)
        logger.info("飞书文档控制器插件初始化中...")

        # 读取配置
        feishu_app_id = config.get('feishu_app_id', '')
        feishu_app_secret = config.get('feishu_app_secret', '')

        if not feishu_app_id or not feishu_app_secret:
            self.feishu_client = None
            logger.warning("⚠️ 未配置飞书 App ID 或 App Secret，"
                           "飞书文档功能将不可用。请在管理面板中配置。")
        else:
            feishu_config = FeishuConfig(
                app_id=feishu_app_id,
                app_secret=feishu_app_secret
            )
            self.feishu_client = FeishuAPIClient(feishu_config)
            logger.info(f"✅ 飞书客户端初始化成功 (App ID: {feishu_app_id[:8]}...)")

        # 注册 FunctionTool 工具
        try:
            tool1 = FindAndSummarizeWikiTool()
            tool1.plugin = self
            tool2 = FindAndReadDocTool()
            tool2.plugin = self
            self.context.add_llm_tools(tool1, tool2)
            logger.info("✅ FunctionTool 工具注册成功")
        except Exception as e:
            logger.warning(f"⚠️ FunctionTool 注册失败: {e}")

        logger.info("飞书文档控制器插件初始化完成")
        logger.info("=" * 50)

    # -----------------------------------------------------------------------
    # 辅助方法
    # -----------------------------------------------------------------------

    async def check_status(self, event: AstrMessageEvent):
        """检查配置状态并验证 API 连接。

        Args:
            event: AstrBot 消息事件
        """
        if not self.feishu_client:
            yield event.plain_result(
                "❌ 飞书文档控制器未配置\n\n"
                "请在管理面板中设置 feishu_app_id 和 feishu_app_secret。\n"
                "获取方式：飞书开放平台 → 创建应用 → 凭证与基础信息"
            )
            return

        try:
            token = await self.feishu_client.get_tenant_access_token()
            yield event.plain_result(
                "✅ 飞书文档控制器运行正常\n\n"
                f"📎 App ID: {self.feishu_client.config.app_id[:8]}...\n"
                f"🔑 Token: {token[:10]}...\n"
                f"🌐 API 地址: {self.feishu_client.base_url}"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 飞书文档控制器连接失败: {e}")

    async def terminate(self):
        """插件卸载时调用，清理资源。"""
        logger.info("飞书文档控制器插件已卸载")

    # -----------------------------------------------------------------------
    # 命令处理方法
    # -----------------------------------------------------------------------

    @filter.command("文档")
    async def document_command(self, event: AstrMessageEvent,
                                action: str = "", param: str = ""):
        """处理 `/文档 [操作] [参数]` 命令。

        支持的操作：搜索、读取、创建、信息、知识库列表、知识库、状态、帮助

        Args:
            event: 消息事件
            action: 操作类型
            param: 操作参数
        """
        # 状态检查
        if action == "状态":
            async for result in self.check_status(event):
                yield result
            return

        # 未配置检查
        if not self.feishu_client:
            yield event.plain_result(
                "❌ 飞书文档控制器未配置\n\n"
                "请在管理面板中设置 feishu_app_id 和 feishu_app_secret。\n"
                "详细配置步骤请参考：/文档 帮助"
            )
            return

        # 无操作提示
        if not action:
            yield event.plain_result(
                "📄 飞书文档控制器\n\n"
                "使用方法：/文档 [操作] [参数]\n\n"
                "常用操作：\n"
                "  - /文档 搜索 [关键词] — 搜索文档\n"
                "  - /文档 读取 [ID/URL] — 读取文档\n"
                "  - /文档 创建 [标题] — 创建文档\n"
                "  - /文档 信息 [ID/URL] — 文档信息\n"
                "  - /文档 知识库列表 — 列出知识库\n"
                "  - /文档 知识库 [ID] — 知识库节点\n"
                "  - /文档 状态 — 检查状态\n"
                "  - /文档 帮助 — 显示帮助"
            )
            return

        try:
            if action == "搜索":
                if not param:
                    yield event.plain_result("❌ 请提供搜索关键词，例如：/文档 搜索 项目计划")
                    return
                items = await self.feishu_client.search_documents(param)
                if not items:
                    yield event.plain_result(f"未找到与 '{param}' 相关的文档")
                    return
                results = [f"🔍 搜索 '{param}' 的结果（共 {len(items)} 条，显示前 10 条）：\n"]
                for i, item in enumerate(items[:10], 1):
                    title = item.get('title', '无标题')
                    doc_id = item.get('doc_id', item.get('token', ''))
                    doc_type = item.get('type', 'docx')
                    url = item.get('url', f"https://bytedance.feishu.cn/docx/{doc_id}" if doc_id else '')
                    results.append(f"{i}. {title} ({doc_type})\n   ID: {doc_id}\n   URL: {url}")
                yield event.plain_result("\n\n".join(results))

            elif action == "读取":
                if not param:
                    yield event.plain_result("❌ 请提供文档 ID 或 URL，例如：/文档 读取 doxcnxxxx")
                    return
                content = await self.feishu_client.get_document_content(param)
                display = content[:1000] + ("\n...(内容已截断)" if len(content) > 1000 else "")
                yield event.plain_result(f"📄 文档内容：\n\n{display}")

            elif action == "创建":
                if not param:
                    yield event.plain_result("❌ 请提供文档标题，例如：/文档 创建 我的笔记")
                    return
                data = await self.feishu_client.create_document(param)
                doc_info = data.get('document', {})
                doc_id = doc_info.get('document_id', '')
                doc_title = doc_info.get('title', param)
                yield event.plain_result(
                    f"✅ 文档创建成功！\n\n"
                    f"📎 标题: {doc_title}\n"
                    f"📎 ID: {doc_id}\n"
                    f"📎 链接: https://bytedance.feishu.cn/docx/{doc_id}"
                )

            elif action == "帮助":
                yield event.plain_result(
                    "📖 飞书文档控制器 - 帮助\n\n"
                    "📎 基本命令：\n"
                    "  /文档 搜索 [关键词] — 搜索文档\n"
                    "  /文档 读取 [ID/URL] — 读取文档内容\n"
                    "  /文档 创建 [标题] — 创建新文档\n"
                    "  /文档 信息 [ID/URL] — 查看文档信息\n"
                    "  /文档 知识库列表 — 列出所有知识库\n"
                    "  /文档 知识库 [ID] — 查看知识库节点\n"
                    "  /文档 状态 — 检查插件状态\n"
                    "  /文档 帮助 — 显示此帮助\n\n"
                    "📎 快捷命令：\n"
                    "  /飞书 搜索 [关键词] — 快捷搜索\n"
                    "  /飞书 读取 [ID/URL] — 快捷读取\n"
                    "  /读取文档 [ID/URL] — 直接读取\n"
                    "  /搜索文档 [关键词] — 直接搜索\n"
                    "  /创建文档 [标题] — 直接创建\n"
                    "  /编辑文档 [ID/URL] [--title 标题] [--content 内容] — 编辑文档\n"
                    "  /删除文档 [ID/URL] — 删除文档\n"
                    "  /复制文档 [token] [--type 类型] [--name 新名称] — 复制文档\n\n"
                    "📎 AI 自动调用：\n"
                    "  配合 AI 助手使用时，AI 可自动调用\n"
                    "  搜索、读取、创建、编辑、复制、导出、\n"
                    "  电子表格读写等工具，无需手动输入命令。\n\n"
                    "📎 配置：\n"
                    "  在管理面板中设置 feishu_app_id 和\n"
                    "  feishu_app_secret 即可开始使用。\n"
                    "  详细权限配置请查看 README.md。"
                )

            elif action == "信息":
                if not param:
                    yield event.plain_result("❌ 请提供文档 ID 或 URL")
                    return
                info = await self.feishu_client.get_document_info(param)
                doc_data = info.get('document', info)
                title = doc_data.get('title', '未知')
                doc_id = doc_data.get('document_id', self.feishu_client._extract_document_id(param))
                create_time = doc_data.get('create_time', '未知')
                update_time = doc_data.get('modify_time', '未知')
                yield event.plain_result(
                    f"📄 文档信息:\n\n"
                    f"  标题: {title}\n"
                    f"  ID: {doc_id}\n"
                    f"  创建时间: {create_time}\n"
                    f"  更新时间: {update_time}\n"
                    f"  链接: https://bytedance.feishu.cn/docx/{doc_id}"
                )

            elif action == "知识库列表":
                spaces = await self.feishu_client.get_wiki_spaces()
                if not spaces:
                    yield event.plain_result("未找到任何知识库")
                    return
                results = ["📚 知识库列表：\n"]
                for i, space in enumerate(spaces[:20], 1):
                    name = space.get('name', '未知')
                    space_id = space.get('space_id', '')
                    results.append(f"{i}. {name}\n   ID: {space_id}")
                yield event.plain_result("\n\n".join(results))

            elif action == "知识库":
                if not param:
                    yield event.plain_result("❌ 请提供知识库 ID，例如：/文档 知识库 123456")
                    return
                nodes = await self.feishu_client.get_wiki_nodes(param)
                if not nodes:
                    yield event.plain_result(f"知识库 {param} 中没有文档节点")
                    return
                results = [f"📁 知识库节点（共 {len(nodes)} 个）：\n"]
                for i, node in enumerate(nodes[:20], 1):
                    title = node.get('title', '无标题')
                    obj_type = node.get('obj_type', '未知')
                    obj_token = node.get('obj_token', '')
                    results.append(f"{i}. [{obj_type}] {title}\n   Token: {obj_token}")
                yield event.plain_result("\n\n".join(results))

            else:
                yield event.plain_result(
                    f"❌ 未知操作: {action}\n"
                    "可用操作: 搜索, 读取, 创建, 信息, 知识库列表, 知识库, 状态, 帮助"
                )

        except Exception as e:
            logger.error(f"命令执行失败 ({action}): {e}")
            yield event.plain_result(f"❌ 操作失败: {e}")

    @filter.command("飞书")
    async def feishu_command(self, event: AstrMessageEvent,
                              action: str = "", param: str = ""):
        """快捷命令 `/飞书 [操作] [参数]`，功能同 /文档 但输出更精简。

        Args:
            event: 消息事件
            action: 操作类型
            param: 操作参数
        """
        if not action:
            yield event.plain_result(
                "📄 飞书快捷命令\n"
                "用法: /飞书 搜索|读取 [参数]\n"
                "详细帮助: /文档 帮助"
            )
            return

        if not self.feishu_client:
            yield event.plain_result("❌ 飞书客户端未配置")
            return

        try:
            if action == "搜索":
                if not param:
                    yield event.plain_result("❌ 请提供关键词")
                    return
                items = await self.feishu_client.search_documents(param)
                if not items:
                    yield event.plain_result(f"未找到 '{param}'")
                    return
                lines = [f"🔍 搜索 '{param}'（前5条）:"]
                for i, item in enumerate(items[:5], 1):
                    title = item.get('title', '无标题')
                    doc_id = item.get('doc_id', item.get('token', ''))
                    lines.append(f"{i}. {title} ({doc_id})")
                yield event.plain_result("\n".join(lines))

            elif action == "读取":
                if not param:
                    yield event.plain_result("❌ 请提供文档 ID 或 URL")
                    return
                content = await self.feishu_client.get_document_content(param)
                display = content[:500] + ("..." if len(content) > 500 else "")
                yield event.plain_result(f"📄 {display}")

            elif action == "创建":
                if not param:
                    yield event.plain_result("❌ 请提供标题")
                    return
                data = await self.feishu_client.create_document(param)
                doc_info = data.get('document', {})
                doc_id = doc_info.get('document_id', '')
                yield event.plain_result(
                    f"✅ 已创建: {doc_info.get('title', param)}\n"
                    f"https://bytedance.feishu.cn/docx/{doc_id}"
                )

            else:
                yield event.plain_result(f"未知操作: {action}（支持: 搜索, 读取, 创建）")

        except Exception as e:
            logger.error(f"飞书快捷命令失败: {e}")
            yield event.plain_result(f"❌ {e}")

    @filter.command("读取文档")
    async def read_doc_command(self, event: AstrMessageEvent, doc_id: str = ""):
        """直接读取文档命令 `/读取文档 [ID/URL]`。

        Args:
            event: 消息事件
            doc_id: 文档 ID 或 URL
        """
        if not doc_id:
            yield event.plain_result("❌ 请提供文档 ID 或 URL，例如：/读取文档 doxcnxxxx")
            return

        if not self.feishu_client:
            yield event.plain_result("❌ 飞书客户端未配置")
            return

        try:
            content = await self.feishu_client.get_document_content(doc_id)
            display = content[:1500] + ("\n...(内容已截断)" if len(content) > 1500 else "")
            yield event.plain_result(f"📄 文档内容：\n\n{display}")
        except Exception as e:
            yield event.plain_result(f"❌ 读取失败: {e}")

    @filter.command("搜索文档")
    async def search_doc_command(self, event: AstrMessageEvent, keyword: str = ""):
        """直接搜索文档命令 `/搜索文档 [关键词]`。

        Args:
            event: 消息事件
            keyword: 搜索关键词
        """
        if not keyword:
            yield event.plain_result("❌ 请提供搜索关键词，例如：/搜索文档 周报")
            return

        if not self.feishu_client:
            yield event.plain_result("❌ 飞书客户端未配置")
            return

        try:
            items = await self.feishu_client.search_documents(keyword)
            if not items:
                yield event.plain_result(f"未找到与 '{keyword}' 相关的文档")
                return

            results = [f"🔍 搜索 '{keyword}'（前 10 条）："]
            for i, item in enumerate(items[:10], 1):
                title = item.get('title', '无标题')
                doc_id = item.get('doc_id', item.get('token', ''))
                url = item.get('url', f"https://bytedance.feishu.cn/docx/{doc_id}" if doc_id else '')
                results.append(f"{i}. {title} (ID: {doc_id}, URL: {url})")
            yield event.plain_result("\n\n".join(results))
        except Exception as e:
            yield event.plain_result(f"❌ 搜索失败: {e}")

    @filter.command("创建文档")
    async def create_doc_command(self, event: AstrMessageEvent, title: str = ""):
        """直接创建文档命令 `/创建文档 [标题]`。

        Args:
            event: 消息事件
            title: 文档标题
        """
        if not title:
            yield event.plain_result("❌ 请提供文档标题，例如：/创建文档 会议纪要")
            return

        if not self.feishu_client:
            yield event.plain_result("❌ 飞书客户端未配置")
            return

        try:
            data = await self.feishu_client.create_document(title)
            doc_info = data.get('document', {})
            doc_id = doc_info.get('document_id', '')
            yield event.plain_result(
                f"✅ 文档创建成功！\n"
                f"标题: {doc_info.get('title', title)}\n"
                f"ID: {doc_id}\n"
                f"链接: https://bytedance.feishu.cn/docx/{doc_id}"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 创建失败: {e}")

    @filter.command("编辑文档")
    async def edit_doc_command(self, event: AstrMessageEvent, doc_id_or_url: str = "", title: str = "", content: str = ""):
        """处理 `/编辑文档 [ID/URL] [--title 新标题] [--content 新内容]` 命令。

        Args:
            event: 消息事件
            doc_id_or_url: 文档 ID 或 URL
            title: 新标题（可选）
            content: 要追加的 Markdown 内容（可选）
        """
        if not self.feishu_client:
            yield event.plain_result("❌ 飞书客户端未配置，无法编辑文档")
            return

        if not doc_id_or_url:
            yield event.plain_result("⚠️ 请提供文档 ID 或 URL\n用法: /编辑文档 [ID] [--title 新标题] [--content 内容]")
            return

        try:
            results = []
            if title:
                await self.feishu_client.update_document_title(doc_id_or_url, title)
                results.append(f"✅ 标题已修改为: {title}")

            if content:
                await self.feishu_client.append_document_content(doc_id_or_url, content)
                results.append("✅ 内容已成功追加到文档末尾")

            if not results:
                yield event.plain_result("⚠️ 未提供标题或内容，无需修改")
                return

            yield event.plain_result("\n".join(results))
        except Exception as e:
            logger.error(f"命令编辑文档失败: {e}")
            yield event.plain_result(f"❌ 编辑失败: {e}")

    @filter.command("删除文档")
    async def delete_doc_command(self, event: AstrMessageEvent, doc_id_or_url: str = ""):
        """处理 `/删除文档 [ID/URL]` 命令。

        Args:
            event: 消息事件
            doc_id_or_url: 文档 ID 或 URL
        """
        if not self.feishu_client:
            yield event.plain_result("❌ 飞书客户端未配置，无法删除文档")
            return

        if not doc_id_or_url:
            yield event.plain_result("⚠️ 请提供文档 ID 或 URL\n用法: /删除文档 [ID/URL]")
            return

        try:
            await self.feishu_client.delete_document(doc_id_or_url)
            yield event.plain_result(f"✅ 文档已移入回收站\n如需恢复，请联系飞书管理员。")
        except Exception as e:
            logger.error(f"命令删除文档失败: {e}")
            yield event.plain_result(f"❌ 删除失败: {e}")

    @filter.command("复制文档")
    async def copy_doc_command(self, event: AstrMessageEvent, file_token: str = "", file_type: str = "docx", name: str = ""):
        """处理 `/复制文档 [token] [--type 类型] [--name 新名称]` 命令。

        Args:
            event: 消息事件
            file_token: 文件 token
            file_type: 文件类型
            name: 新文件名
        """
        if not self.feishu_client:
            yield event.plain_result("❌ 飞书客户端未配置")
            return
        if not file_token:
            yield event.plain_result("⚠️ 请提供文件 token\n用法: /复制文档 [token] [--type 类型] [--name 新名称]")
            return

        try:
            data = await self.feishu_client.copy_document(file_token, file_type, name)
            new_token = data.get('file_token', '')
            yield event.plain_result(f"✅ 文件复制成功\n新文件链接: https://bytedance.feishu.cn/docx/{new_token}")
        except Exception as e:
            logger.error(f"命令复制文档失败: {e}")
            yield event.plain_result(f"❌ 复制失败: {e}")

    # -----------------------------------------------------------------------
    # LLM Tool 方法（@filter.llm_tool 装饰器）
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="read_feishu_doc")
    async def read_feishu_doc(self, event: AstrMessageEvent, doc_id_or_url: str) -> str:
        """读取飞书文档原始内容（LLM Tool）。

        当 AI 需要阅读某个飞书文档时调用此工具。
        支持普通 docx 文档 ID 和 Wiki 节点 token。
        返回内容同时包含文档链接供用户确认。

        Args:
            doc_id_or_url(string): 文档 ID 或 URL（支持 docx ID、wiki token、完整 URL）

        Returns:
            文档内容（最多 10000 字符），末尾带处理提示
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置，无法读取文档"

        try:
            doc_id = self.feishu_client._extract_document_id(doc_id_or_url)
            content = await self.feishu_client.get_document_content(doc_id_or_url)
            if len(content) > 10000:
                content = content[:10000] + "\n...(内容已截断)"

            # 生成正确的文档链接
            if '/wiki/' in doc_id_or_url:
                doc_url = doc_id_or_url
            elif doc_id and len(doc_id) >= 20 and not doc_id.startswith('doxcn'):
                doc_url = f"https://my.feishu.cn/wiki/{doc_id}"
            else:
                doc_url = f"https://bytedance.feishu.cn/docx/{doc_id}"

            return (
                f"📎 文档链接: {doc_url}\n"
                f"📎 文档 ID: {doc_id}\n\n"
                f"{content}\n\n"
                f"---\n以上是文档的原始内容。"
                "请根据用户的请求对内容进行二次处理后再回复用户。"
            )
        except Exception as e:
            logger.error(f"LLM Tool read_feishu_doc 失败: {e}")
            return f"❌ 读取失败: {e}"

    @filter.llm_tool(name="search_feishu_docs")
    async def search_feishu_docs(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """搜索飞书文档（LLM Tool）。

        当 AI 需要搜索文档时调用此工具。

        Args:
            keyword(string): 搜索关键词

        Returns:
            搜索结果（前 10 条），标注可用文档 ID 和链接
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置，无法搜索文档"

        try:
            items = await self.feishu_client.search_documents(keyword)
            if not items:
                return f"未找到与 '{keyword}' 相关的文档"

            results = [f"🔍 搜索 '{keyword}' 的结果（共 {len(items)} 条，显示前 10 条）：\n"]
            for i, item in enumerate(items[:10], 1):
                title = item.get('title', '无标题')
                doc_id = item.get('doc_id', item.get('token', ''))
                url = item.get('url', '')
                if not url and doc_id:
                    if doc_id.startswith('doxcn'):
                        url = f"https://bytedance.feishu.cn/docx/{doc_id}"
                    else:
                        url = f"https://my.feishu.cn/wiki/{doc_id}"
                results.append(f"【{i}】{title}\n    ID: {doc_id}\n    链接: {url}")

            results.append(
                "\n---\n如需读取某个文档，请使用 read_feishu_doc 工具并提供文档 ID 或链接。"
            )
            return "\n".join(results)
        except Exception as e:
            logger.error(f"LLM Tool search_feishu_docs 失败: {e}")
            return f"❌ 搜索失败: {e}"

    @filter.llm_tool(name="find_and_read_doc")
    async def find_and_read_doc(self, event: AstrMessageEvent, keyword: str) -> str:
        """搜索关键词并读取第一个匹配文档的内容（LLM Tool）。

        一步完成搜索+读取，方便 AI 快速获取文档内容。

        Args:
            keyword(string): 搜索关键词

        Returns:
            文档内容（最多 10000 字符），含文档标题和链接
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            items = await self.feishu_client.search_documents(keyword)
            if not items:
                return f"未找到与 '{keyword}' 相关的文档"

            first_doc = items[0]
            doc_title = first_doc.get('title', '无标题')
            doc_id = first_doc.get('doc_id', first_doc.get('token', ''))

            if not doc_id:
                return f"找到文档 '{doc_title}'，但无法获取文档 ID"

            content = await self.feishu_client.get_document_content(doc_id)
            if len(content) > 10000:
                content = content[:10000] + "\n...(内容已截断)"

            # 生成正确格式的链接
            if doc_id.startswith('doxcn'):
                doc_url = f"https://bytedance.feishu.cn/docx/{doc_id}"
            else:
                doc_url = f"https://my.feishu.cn/wiki/{doc_id}"

            return (
                f"📄 文档: {doc_title}\n"
                f"📎 链接: {doc_url}\n"
                f"📎 文档 ID: {doc_id}\n\n"
                f"{content}\n\n"
                f"---\n以上是文档的原始内容。"
                "请根据用户的请求对内容进行二次处理后再回复用户。"
            )
        except Exception as e:
            logger.error(f"LLM Tool find_and_read_doc 失败: {e}")
            return f"❌ 操作失败: {e}"

    @filter.llm_tool(name="create_feishu_doc")
    async def create_feishu_doc(self, event: AstrMessageEvent, title: str) -> str:
        """创建飞书文档（LLM Tool）。

        当 AI 需要创建新文档时调用此工具。

        Args:
            title(string): 文档标题

        Returns:
            创建结果，含标题、ID 和链接
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            data = await self.feishu_client.create_document(title)
            doc_info = data.get('document', {})
            doc_id = doc_info.get('document_id', '')
            return (
                f"✅ 文档创建成功\n"
                f"标题: {doc_info.get('title', title)}\n"
                f"ID: {doc_id}\n"
                f"链接: https://bytedance.feishu.cn/docx/{doc_id}"
            )
        except Exception as e:
            logger.error(f"LLM Tool create_feishu_doc 失败: {e}")
            return f"❌ 创建失败: {e}"

    @filter.llm_tool(name="list_feishu_wikis")
    async def list_feishu_wikis(self, event: AstrMessageEvent) -> str:
        """列出所有知识库（LLM Tool）。

        当 AI 需要展示可用知识库时调用此工具。
        返回带序号的列表，方便用户说"第X个知识库"。

        Returns:
            知识库名称和 ID 列表（带序号）
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            spaces = await self.feishu_client.get_wiki_spaces()
            if not spaces:
                return "未找到任何知识库"

            results = [f"📚 知识库列表（共 {len(spaces)} 个）：\n"]
            for i, space in enumerate(spaces[:30], 1):
                name = space.get('name', '未知')
                space_id = space.get('space_id', '')
                description = space.get('description', '')
                desc_str = f" - {description}" if description else ""
                results.append(f"【{i}】{name}{desc_str}\n     space_id: {space_id}")

            results.append(
                "\n---\n"
                "📌 如需查看某个知识库的文档节点，请使用 get_feishu_wiki_nodes "
                "并提供对应的 space_id。"
            )
            return "\n".join(results)
        except Exception as e:
            logger.error(f"LLM Tool list_feishu_wikis 失败: {e}")
            return f"❌ 获取失败: {e}"

    @filter.llm_tool(name="get_feishu_wiki_nodes")
    async def get_feishu_wiki_nodes(self, event: AstrMessageEvent, space_id: str) -> str:
        """获取知识库文档节点列表（LLM Tool）。

        当用户说"第X个文档"、"第X个模板"时，请用本工具先列出所有节点，
        然后根据序号找到对应文档的 node_token 或 obj_token。

        Args:
            space_id(string): 知识库空间 ID

        Returns:
            带序号的节点列表（最多 30 条），每个节点含序号、标题、类型、链接
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            nodes = await self.feishu_client.get_wiki_nodes(space_id)
            if not nodes:
                return "该知识库中没有文档节点"

            max_show = min(len(nodes), 30)
            results = [f"📁 知识库节点列表（共 {len(nodes)} 个，显示前 {max_show} 个）：\n"]

            for i, node in enumerate(nodes[:max_show], 1):
                title = node.get('title', '无标题')
                obj_type = node.get('obj_type', '未知')
                obj_token = node.get('obj_token', '')
                node_token = node.get('node_token', '')
                has_child = node.get('has_child', False)
                # Wiki 节点使用 node_token 构建 URL
                wiki_url = f"https://my.feishu.cn/wiki/{node_token}" if node_token else (
                    f"https://bytedance.feishu.cn/docx/{obj_token}" if obj_token else "无链接"
                )
                child_mark = " 📂(含子节点)" if has_child else ""
                results.append(
                    f"【{i}】[{obj_type}] {title}{child_mark}\n"
                    f"     node_token: {node_token}\n"
                    f"     obj_token: {obj_token}\n"
                    f"     链接: {wiki_url}"
                )

            if len(nodes) > max_show:
                results.append(f"\n⚠️ 仅显示前 {max_show} 个节点，共 {len(nodes)} 个。")

            results.append(
                "\n---\n"
                "📌 使用说明：\n"
                '- 用户说「第2个模板」时，对应上方【2】号节点\n'
                "- 用 node_token 或 obj_token 调用 read_feishu_doc 读取内容\n"
                '- 用上方「链接」可以直接在浏览器打开文档'
            )
            return "\n".join(results)
        except Exception as e:
            logger.error(f"LLM Tool get_feishu_wiki_nodes 失败: {e}")
            return f"❌ 获取失败: {e}"

    @filter.llm_tool(name="summarize_wiki_content")
    async def summarize_wiki_content(self, event: AstrMessageEvent, space_id: str) -> str:
        """遍历知识库中所有文档并汇总内容（LLM Tool）。

        最多读取 10 个文档，每个文档附带序号、标题和链接，
        返回原始内容供 AI 二次处理。

        Args:
            space_id(string): 知识库空间 ID

        Returns:
            汇总的文档内容（含序号和链接）
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            nodes = await self.feishu_client.get_wiki_nodes(space_id)
            if not nodes:
                return "该知识库中没有文档节点"

            results = [f"📚 知识库内容汇总（共 {len(nodes)} 个节点）："]
            count = 0

            for i, node in enumerate(nodes[:10], 1):
                title = node.get('title', '无标题')
                obj_token = node.get('obj_token', '')
                node_token = node.get('node_token', '')
                obj_type = node.get('obj_type', '未知')
                wiki_url = f"https://my.feishu.cn/wiki/{node_token}" if node_token else (
                    f"https://bytedance.feishu.cn/docx/{obj_token}" if obj_token else "无链接"
                )

                if not obj_token:
                    results.append(f"\n【{i}】📄 {title} [{obj_type}]: 无法获取内容（缺少 token）")
                    continue

                try:
                    content = await self.feishu_client.get_document_content(obj_token)
                    if len(content) > 2000:
                        content = content[:2000] + "\n...(已截断)"
                    results.append(
                        f"\n【{i}】📄 {title} [{obj_type}]\n"
                        f"    链接: {wiki_url}\n"
                        f"    obj_token: {obj_token}\n"
                        f"{content}"
                    )
                    count += 1
                except Exception as e:
                    results.append(
                        f"\n【{i}】📄 {title} [{obj_type}]\n"
                        f"    链接: {wiki_url}\n"
                        f"    读取失败: {e}"
                    )

            results.append(
                f"\n\n---\n已汇总 {count} 个文档的内容。"
                '每个文档上方标注了序号【N】，用户说「第N个」时请对应序号。'
                "请根据用户的请求对以上内容进行分析、总结或回答。"
            )
            return "\n".join(results)
        except Exception as e:
            logger.error(f"LLM Tool summarize_wiki_content 失败: {e}")
            return f"❌ 汇总失败: {e}"

    @filter.llm_tool(name="edit_feishu_doc")
    async def edit_feishu_doc(self, event: AstrMessageEvent, doc_id_or_url: str, title: str = "", content: str = "") -> str:
        """编辑飞书文档的标题或追加内容（LLM Tool）。

        当 AI 需要修改文档标题或向文档追加内容时调用此工具。

        Args:
            doc_id_or_url(string): 文档 ID 或 URL
            title(string): 新标题（可选，为空则不修改标题）
            content(string): 要追加的 Markdown 内容（可选，为空则不追加）

        Returns:
            编辑结果
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置，无法编辑文档"

        try:
            results = []
            if title:
                await self.feishu_client.update_document_title(doc_id_or_url, title)
                results.append(f"✅ 标题已修改为: {title}")

            if content:
                await self.feishu_client.append_document_content(doc_id_or_url, content)
                results.append("✅ 内容已成功追加到文档末尾")

            if not results:
                return "⚠️ 未提供标题或内容，无需修改"

            return "\n".join(results)
        except Exception as e:
            logger.error(f"LLM Tool edit_feishu_doc 失败: {e}")
            return f"❌ 编辑失败: {e}"

    @filter.llm_tool(name="delete_feishu_doc")
    async def delete_feishu_doc(self, event: AstrMessageEvent, doc_id_or_url: str) -> str:
        """将飞书文档移入回收站（LLM Tool）。

        当 AI 需要删除飞书文档时调用此工具。删除后文档可在回收站中恢复。

        Args:
            doc_id_or_url(string): 文档 ID 或 URL

        Returns:
            删除结果，含回收站链接
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置，无法删除文档"

        try:
            await self.feishu_client.delete_document(doc_id_or_url)
            return (
                f"✅ 文档已移入回收站\n"
                f"如需恢复，请联系飞书管理员从回收站中找回。"
            )
        except Exception as e:
            logger.error(f"LLM Tool delete_feishu_doc 失败: {e}")
            return f"❌ 删除失败: {e}"

    # -----------------------------------------------------------------------
    # LLM Tool — 列出电子表格的 Sheet
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="list_feishu_sheet_sheets")
    async def list_feishu_sheet_sheets(self, event: AstrMessageEvent, spreadsheet_token: str) -> str:
        """列出飞书电子表格中的所有 Sheet（LLM Tool）。

        在读取或写入电子表格前，先用此工具了解有哪些 Sheet 可用。

        Args:
            spreadsheet_token(string): 电子表格 token

        Returns:
            Sheet 列表（含 sheet_id 和标题）
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            sheets = await self.feishu_client.get_spreadsheet_sheets(spreadsheet_token)
            if not sheets:
                return "该电子表格中没有 Sheet"

            results = [f"📊 电子表格 Sheet 列表（共 {len(sheets)} 个）：\n"]
            for i, s in enumerate(sheets[:20], 1):
                sheet_id = s.get('sheet_id', '')
                title = s.get('title', '无标题')
                row_count = s.get('row_count', s.get('grid_properties', {}).get('row_count', '?'))
                col_count = s.get('column_count', s.get('grid_properties', {}).get('column_count', '?'))
                results.append(f"【{i}】{title} (sheet_id: {sheet_id}, {row_count}行×{col_count}列)")

            results.append(
                "\n---\n"
                "📌 读取数据请使用 read_feishu_sheet，格式如：\n"
                "   spreadsheet_token + range_（如 Sheet1!A1:D20）"
            )
            return "\n".join(results)
        except Exception as e:
            logger.error(f"LLM Tool list_feishu_sheet_sheets 失败: {e}")
            return f"❌ 获取失败: {e}"

    # -----------------------------------------------------------------------
    # LLM Tool — 读取电子表格
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="read_feishu_sheet")
    async def read_feishu_sheet(self, event: AstrMessageEvent, spreadsheet_token: str, range_: str = "") -> str:
        """读取飞书电子表格数据（LLM Tool）。

        当 AI 需要查看电子表格内容时调用此工具。
        如果不确定有哪些 Sheet，先用 list_feishu_sheet_sheets 工具。

        Args:
            spreadsheet_token(string): 电子表格 token
            range_(string): 读取范围，如 "Sheet1!A1:C10" 或 "Sheet1"（读全表），为空则自动读第一个 Sheet

        Returns:
            表格数据文本
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            if not range_:
                # 自动获取第一个 sheet
                sheets = await self.feishu_client.get_spreadsheet_sheets(spreadsheet_token)
                if not sheets:
                    return "❌ 该电子表格中没有 Sheet，无法自动读取"
                first_sheet = sheets[0]
                sheet_id = first_sheet.get('sheet_id', 'Sheet1')
                range_ = f"{sheet_id}!A1:ZZ500"
            else:
                sheet_id, cell_range = self.feishu_client._parse_sheet_range(range_)
                range_ = f"{sheet_id}!{cell_range}"

            sheet_id, cell_range = self.feishu_client._parse_sheet_range(range_)
            data = await self.feishu_client.read_spreadsheet_cells(spreadsheet_token, sheet_id, cell_range)

            if not data:
                return f"📊 表格为空 ({range_})"

            # 格式化输出（前 1000 行）
            lines = [f"📊 表格数据 (sheet={sheet_id}, {len(data)} 行):"]
            for i, row in enumerate(data[:1000], 1):
                lines.append(f"  {i}: " + " | ".join(str(c) for c in row))
            if len(data) > 1000:
                lines.append(f"  ...（共 {len(data)} 行，仅显示前 1000 行）")

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"LLM Tool read_feishu_sheet 失败: {e}")
            return f"❌ 读取失败: {e}"

    # -----------------------------------------------------------------------
    # LLM Tool — 写入电子表格
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="write_feishu_sheet")
    async def write_feishu_sheet(self, event: AstrMessageEvent, spreadsheet_token: str, range_: str, values_text: str) -> str:
        """向飞书电子表格写入数据（LLM Tool）。

        每行用换行分隔，每列用逗号分隔。

        Args:
            spreadsheet_token(string): 电子表格 token
            range_(string): 写入范围，如 "Sheet1!A1"
            values_text(string): 要写入的数据，行用换行分隔，列用逗号分隔

        Returns:
            写入结果
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            sheet_id, cell_range = self.feishu_client._parse_sheet_range(range_)
            values = [row.split(",") for row in values_text.strip().split("\n") if row.strip()]
            await self.feishu_client.write_spreadsheet_cells(spreadsheet_token, sheet_id, cell_range, values)
            return f"✅ 数据已写入 {spreadsheet_token} 的 {sheet_id}!{cell_range}（{len(values)} 行）"
        except Exception as e:
            logger.error(f"LLM Tool write_feishu_sheet 失败: {e}")
            return f"❌ 写入失败: {e}"

    # -----------------------------------------------------------------------
    # LLM Tool — 追加电子表格数据
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="append_feishu_sheet")
    async def append_feishu_sheet(self, event: AstrMessageEvent, spreadsheet_token: str, range_: str, values_text: str) -> str:
        """向飞书电子表格末尾追加数据（LLM Tool）。

        Args:
            spreadsheet_token(string): 电子表格 token
            range_(string): 参考范围，如 "Sheet1!A1:B1"
            values_text(string): 要追加的数据，行用换行分隔，列用逗号分隔

        Returns:
            追加结果
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            values = [row.split(",") for row in values_text.strip().split("\n") if row.strip()]
            await self.feishu_client.append_values(spreadsheet_token, range_, values)
            return f"✅ 数据已追加到 {spreadsheet_token}（{len(values)} 行）"
        except Exception as e:
            logger.error(f"LLM Tool append_feishu_sheet 失败: {e}")
            return f"❌ 追加失败: {e}"

    # -----------------------------------------------------------------------
    # LLM Tool — 复制文档
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="copy_feishu_doc")
    async def copy_feishu_doc(self, event: AstrMessageEvent, file_token: str = "", file_type: str = "docx", name: str = "") -> str:
        """复制飞书文档（LLM Tool）。

        Args:
            file_token(string): 源文件 token
            file_type(string): 文件类型，如 docx/sheet/bitable
            name(string): 新文件名（可选）

        Returns:
            复制结果
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"
        if not file_token:
            return "⚠️ 请提供文件 token"

        try:
            data = await self.feishu_client.copy_document(file_token, file_type, name)
            new_token = data.get('file_token', '')
            return f"✅ 文件复制成功\n新文件链接: https://bytedance.feishu.cn/docx/{new_token}"
        except Exception as e:
            logger.error(f"LLM Tool copy_feishu_doc 失败: {e}")
            return f"❌ 复制失败: {e}"

    # -----------------------------------------------------------------------
    # LLM Tool — 导出文档
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="export_feishu_doc")
    async def export_feishu_doc(self, event: AstrMessageEvent, file_token: str, file_type: str, export_format: str = "pdf") -> str:
        """导出飞书文档为指定格式（LLM Tool）。

        支持的导出格式（取决于 file_type）：
        - docx/doc 类型: pdf、docx
        - sheet 类型: pdf、xlsx、csv
        - bitable 类型: pdf、xlsx、csv
        注意：飞书 API 不支持导出为 txt 或 md 格式。

        Args:
            file_token(string): 文件 token
            file_type(string): 源文件类型，如 docx/sheet/bitable
            export_format(string): 导出格式，如 pdf/docx/xlsx/csv

        Returns:
            导出结果
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            await self.feishu_client.export_document(file_token, file_type, export_format)
            return f"✅ 导出任务已提交（{file_token} → {export_format}），请稍后查看导出结果"
        except Exception as e:
            logger.error(f"LLM Tool export_feishu_doc 失败: {e}")
            return f"❌ 导出失败: {e}"

    # -----------------------------------------------------------------------
    # LLM Tool — 列出文件夹内容
    # -----------------------------------------------------------------------

    @filter.llm_tool(name="list_feishu_folder")
    async def list_feishu_folder(self, event: AstrMessageEvent, folder_token: str = "") -> str:
        """列出飞书文件夹中的文件清单（LLM Tool）。

        不传 folder_token 时列出根目录（我的空间）。

        Args:
            folder_token(string): 文件夹 token（可选，为空时查询根目录）

        Returns:
            文件清单
        """
        if not self.feishu_client:
            return "❌ 飞书客户端未配置"

        try:
            if not folder_token:
                root = await self.feishu_client.get_root_folder_meta()
                folder_token = root.get('token', '')
            items = await self.feishu_client.get_folder_files(folder_token)
            if not items:
                return "📂 该文件夹为空"
            results = [f"📂 文件夹内容（共 {len(items)} 项）："]
            for i, item in enumerate(items[:30], 1):
                name = item.get('name', '无名称')
                item_type = item.get('type', '未知')
                item_token = item.get('file_token', item.get('token', ''))
                results.append(f"{i}. [{item_type}] {name} (token: {item_token})")
            return "\n".join(results)
        except Exception as e:
            logger.error(f"LLM Tool list_feishu_folder 失败: {e}")
            return f"❌ 列出失败: {e}"

"""
Prompt template manager

Loads prompt templates from YAML files and substitutes variables.
Ngôn ngữ prompt mặc định là tiếng Anh; có thể chọn tiếng Việt bằng LLM_LANGUAGE.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from alicecore.exceptions import PromptError
from alicecore.utils import get_logger

logger = get_logger("prompt.manager")


class PromptTemplate:
    """Prompt template"""

    def __init__(
        self,
        name: str,
        template: str,
        variables: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> None:
        self.name = name
        self.template = template
        self.variables = variables or []
        self.description = description

    def render(self, **kwargs: Any) -> str:
        """
        Render the template

        Args:
            **kwargs: variable values

        Returns:
            The rendered text

        Raises:
            PromptError: a required variable is missing
        """
        missing = set(self.variables) - set(kwargs.keys())
        if missing:
            raise PromptError(f"Template '{self.name}' is missing required variables: {', '.join(missing)}")

        try:
            return self.template.format(**kwargs)
        except KeyError as e:
            raise PromptError(f"Template variable error: {e}") from e
        except Exception as e:
            raise PromptError(f"Template rendering failed: {e}") from e


class PromptManager:
    """Prompt manager (multi-language)"""

    def __init__(self, prompts_dir: Optional[Path] = None) -> None:
        """
        Initialise the prompt manager

        Args:
            prompts_dir: path to the prompts directory

        Hỗ trợ ngôn ngữ:
            - Tiếng Anh (en) là bản gốc trong prompts/ và là mặc định.
            - Tiếng Việt (vi) ưu tiên prompts/vi/, thiếu file thì dùng bản tiếng Anh.
            - Cấu hình qua LLM_LANGUAGE hoặc Settings.llm_language.
        """
        if prompts_dir is None:
            current_file = Path(__file__)
            project_root = current_file.parent.parent.parent.parent
            prompts_dir = project_root / "prompts"

        self.prompts_dir = Path(prompts_dir)
        self.templates: Dict[str, PromptTemplate] = {}
        self.template_data: Dict[str, Dict[str, Any]] = {}

        self.language = self._get_language()

        if self.prompts_dir.exists():
            self.load_templates()
            logger.info(
                "Prompt manager initialised",
                extra={
                    "prompts_dir": str(self.prompts_dir),
                    "language": self.language,
                    "count": len(self.templates),
                },
            )
        else:
            logger.warning(f"The prompts directory does not exist: {self.prompts_dir}")

    #: Ngôn ngữ prompt được hỗ trợ. Thêm ngôn ngữ mới = thêm mã ở đây + tạo
    #: prompts/<mã>/*.yaml. Thiếu file nào thì tự bù bằng bản gốc tiếng Anh.
    #:
    #: Mặc định là "en" một cách CÓ CHỦ Ý: prompt trích xuất bằng tiếng Anh cho
    #: LLM tuân thủ JSON schema ổn định hơn, kể cả khi nội dung đầu vào là tiếng
    #: Việt. Ngôn ngữ giao diện là chuyện khác, không liên quan tới biến này.
    SUPPORTED_LANGUAGES = ("en", "vi")
    DEFAULT_LANGUAGE = "en"

    def _get_language(self) -> str:
        """Ưu tiên: Settings > biến môi trường LLM_LANGUAGE > mặc định."""
        try:
            from alicecore.core.config import get_settings
            lang = (get_settings().llm_language or "").lower()
        except Exception:
            lang = os.getenv("LLM_LANGUAGE", self.DEFAULT_LANGUAGE).lower()
        if lang not in self.SUPPORTED_LANGUAGES:
            logger.warning(
                "Ngôn ngữ '%s' chưa hỗ trợ; dùng '%s'. Hỗ trợ: %s",
                lang, self.DEFAULT_LANGUAGE, ", ".join(self.SUPPORTED_LANGUAGES),
            )
            return self.DEFAULT_LANGUAGE
        return lang

    def load_templates(self) -> None:
        """Nạp prompt: thư mục ngôn ngữ đè lên bản gốc tiếng Anh ở thư mục cha.

        Thứ tự:
          1. prompts/<language>/*.yaml — bản dịch riêng (nếu có), thắng.
          2. prompts/*.yaml            — BẢN GỐC TIẾNG ANH, bù mọi template còn thiếu.

        Nhờ vậy một ngôn ngữ mới chỉ cần dịch phần muốn đổi; file nào chưa dịch
        vẫn chạy tiếng Anh chứ không gãy.
        """
        if not self.prompts_dir.exists():
            logger.warning("Không thấy thư mục prompt: %s", self.prompts_dir)
            return

        overridden = 0
        lang_dir = self.prompts_dir / self.language
        if lang_dir.exists():
            for yaml_file in sorted(lang_dir.glob("*.yaml")):
                try:
                    self._load_yaml_file(yaml_file)
                    overridden += 1
                except Exception as e:
                    logger.error("Lỗi nạp prompt %s: %s", yaml_file, e, exc_info=True)

        if self.language != "en" and not overridden:
            logger.info(
                "Chưa có bộ prompt riêng cho '%s' — dùng bản gốc tiếng Anh. "
                "Đây là mặc định an toàn, không phải lỗi.", self.language,
            )

        # Bản gốc tiếng Anh: luôn nạp, chỉ bù template chưa có.
        for yaml_file in sorted(self.prompts_dir.glob("*.yaml")):
            try:
                self._load_yaml_file(yaml_file, skip_existing=True)
            except Exception as e:
                logger.error("Lỗi nạp prompt %s: %s", yaml_file, e, exc_info=True)

    def _load_yaml_file(
        self, yaml_file: Path, skip_existing: bool = False, template_name: Optional[str] = None
    ) -> None:
        """Load templates from a YAML file"""
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            logger.warning(f"Invalid YAML format: {yaml_file}")
            return

        for name, config in data.items():
            if not isinstance(config, dict):
                continue

            final_template_name = template_name if template_name else name

            if skip_existing and final_template_name in self.templates:
                continue

            self.template_data[final_template_name] = config.copy()

            template_text = config.get("template", "") or config.get("system", "")
            variables = config.get("variables", [])
            description = config.get("description", "")

            template = PromptTemplate(
                name=final_template_name,
                template=template_text,
                variables=variables,
                description=description,
            )

            self.templates[final_template_name] = template
            logger.debug(f"Loaded template: {final_template_name}")

    def get(self, name: str) -> PromptTemplate:
        """Get a template"""
        if name not in self.templates:
            raise PromptError(f"Template does not exist: {name}")
        return self.templates[name]

    def render(self, name: str, **kwargs: Any) -> str:
        """Render a template"""
        template = self.get(name)
        return template.render(**kwargs)

    def has(self, name: str) -> bool:
        """Check whether a template exists"""
        return name in self.templates

    def list_templates(self) -> List[str]:
        """List every template name"""
        return list(self.templates.keys())

    def get_template_config(self, name: str, *, test_mode: bool = False) -> Dict[str, Any]:
        """
        Get the full configuration data of a template (from the YAML file, with test mode support)

        Args:
            name: template name
            test_mode: whether to use the test version (reads the test_{name} configuration)

        Returns:
            The full template configuration dictionary

        Raises:
            PromptError: the template configuration does not exist
        """
        if test_mode:
            test_name = f"test_{name}"
            if test_name in self.template_data:
                logger.info(f"Using the test template: {test_name}")
                name = test_name
            else:
                logger.warning(f"The test template does not exist: {test_name}, using the default template: {name}")

        if name not in self.template_data:
            self.load_templates()
            if name not in self.template_data:
                raise PromptError(f"Template configuration does not exist: {name}")

        return self.template_data[name]


# Global manager instance (singleton)
_prompt_manager: Optional[PromptManager] = None


def get_prompt_manager() -> PromptManager:
    """Get the global prompt manager"""
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager


def reset_prompt_manager() -> None:
    """Reset the global prompt manager"""
    global _prompt_manager
    _prompt_manager = None

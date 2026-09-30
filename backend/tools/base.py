from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type
from pydantic import BaseModel, ValidationError
import logging

logger = logging.getLogger("tools")

class ToolResult(BaseModel):
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    tool_name: str
    metadata: Dict[str, Any] = {}

class BaseTool(ABC):
    name: str
    description: str
    args_schema: Type[BaseModel]

    def execute(self, **kwargs) -> ToolResult:
        try:
            validated_args = self.args_schema(**kwargs)
            return self._run(validated_args)
        except ValidationError as ve:
            logger.warning(f"Validation error in tool {self.name}: {ve}")
            return ToolResult(
                success=False,
                error=f"Schema validation error: {str(ve)}",
                tool_name=self.name
            )
        except Exception as e:
            logger.error(f"Execution error in tool {self.name}: {e}")
            return ToolResult(
                success=False,
                error=f"Internal tool error: {str(e)}",
                tool_name=self.name
            )

    @abstractmethod
    def _run(self, args: BaseModel) -> ToolResult:
        pass

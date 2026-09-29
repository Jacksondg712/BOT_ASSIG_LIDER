from typing import Optional

from pydantic import BaseModel


class SalesforceUser(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    title: Optional[str] = None


class LeadAssignmentResult(BaseModel):
    lead_id: str
    lead_name: str
    action: str  # "processed" | "skipped" | "error"
    reason: str

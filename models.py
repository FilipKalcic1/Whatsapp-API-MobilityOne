"""
Database Models - Production Ready
"""

from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class UserMapping(Base):
    """
    Maps WhatsApp phone numbers to MobilityOne PersonIds.
    
    This allows fast lookups without hitting the API every time.
    """
    __tablename__ = "user_mappings"
    
    phone_number = Column(String(50), primary_key=True, index=True)
    api_identity = Column(String(100), nullable=False)  # PersonId (GUID)
    display_name = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<UserMapping {self.phone_number} -> {self.api_identity[:8]}...>"


class ConversationLog(Base):
    """
    Log of all conversations for analytics and debugging.
    """
    __tablename__ = "conversation_logs"
    
    id = Column(String(50), primary_key=True)
    phone_number = Column(String(50), index=True)
    direction = Column(String(10))  # 'inbound' or 'outbound'
    content = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
    tool_called = Column(String(100), nullable=True)
    api_response_time = Column(String(20), nullable=True)
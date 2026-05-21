# app/ofx_schema.py
from pydantic import BaseModel, Field
from typing import List, Optional

class AccountInfo(BaseModel):
    bank: str
    holder: Optional[str] = "Não identificado"
    card_last4: Optional[str] = "0000"

class StandardTransaction(BaseModel):
    id: str = Field(..., description="ID determinístico baseado no hash dos dados da transação")
    date: str = Field(..., description="Data no formato ISO YYYY-MM-DD")
    description: str
    amount: float = Field(..., description="Valor negativo para despesas")
    currency: str = "BRL"
    category_raw: str
    category: str
    type: str = "CREDIT"
    payment_method: str = "credito"

class OpenFinancePayload(BaseModel):
    source: str = "c6_pdf"
    account: AccountInfo
    transactions: List[StandardTransaction]
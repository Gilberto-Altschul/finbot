# app/ofx_schema.py
from pydantic import BaseModel, Field
from typing import List, Optional, Literal

class AccountInfo(BaseModel):
    bank: str
    holder: Optional[str] = "Não identificado"
    card_last4: Optional[str] = "0000"

class StandardTransaction(BaseModel):
    id: str = Field(..., description="ID determinístico baseado no hash dos dados da transação")
    date: str = Field(..., description="Data no formato ISO YYYY-MM-DD")
    description: str
    amount: float = Field(..., description="Valor positivo para despesas")
    currency: str = "BRL"
    category_raw: Optional[str] = "Não identificado"
    category: str
    subcategory: Optional[str] = "Geral"
    installment_of: Optional[int] = None
    installment_total: Optional[int] = None
    type: Literal["expense", "income"] = "expense" # Permite 'expense' ou 'income'
    payment_method: str = "credito"
    billing_date: Optional[str] = None  # Data de vencimento da fatura (crédito) ou data da compra (débito)

class OpenFinancePayload(BaseModel):
    source: str = "pdf"
    account: AccountInfo = Field(default_factory=lambda: AccountInfo(bank="Não identificado"))
    transactions: List[StandardTransaction]
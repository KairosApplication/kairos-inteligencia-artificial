"""Permissões determinadas pelo servidor, independentes das decisões do LLM."""
from dataclasses import dataclass
from hashlib import sha256

from data.request_context import chat_context


@dataclass(frozen=True)
class Principal:
    user_id: int
    role: str
    customer_id: int | None = None
    sector_ids: tuple[int, ...] = ()

    @property
    def scope(self):
        value = f"v1:{self.user_id}:{self.role}:{self.customer_id}:{sorted(set(self.sector_ids))}"
        return sha256(value.encode()).hexdigest()

    def allows(self, domain):
        return (domain == "faq" or self.role == "MANAGER"
                or (self.role == "STOCKER" and domain == "estoque")
                or (self.role == "CUSTOMER" and domain == "clientes"))


def principal():
    context = chat_context.get()
    return context.principal if context else None


def require_domain(domain):
    identity = principal()
    # Console local e integração interna continuam sendo interfaces confiáveis.
    if identity is not None and not identity.allows(domain):
        raise PermissionError("Seu perfil não permite acessar estes dados.")
    return identity


def customer_scope(customer_id=None):
    identity = require_domain("clientes")
    if identity is not None and identity.role != "MANAGER":
        if identity.customer_id is None or (customer_id is not None and customer_id != identity.customer_id):
            raise PermissionError("Você só pode consultar seus próprios dados.")
        return identity.customer_id
    return customer_id


def stock_sectors():
    identity = require_domain("estoque")
    if identity is not None and identity.role != "MANAGER":
        if not identity.sector_ids:
            raise PermissionError("Nenhum setor de reposição foi atribuído ao seu usuário.")
        return list(identity.sector_ids)
    return None

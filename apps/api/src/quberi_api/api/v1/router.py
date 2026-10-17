from fastapi import APIRouter

from quberi_api.api.v1.admin.ai_providers import admin_ai_providers_router
from quberi_api.api.v1.admin.auth import admin_auth_router
from quberi_api.api.v1.admin.organizations import router as admin_organizations_router
from quberi_api.api.v1.admin.stats import admin_stats_router
from quberi_api.api.v1.external import external_router
from quberi_api.api.v1.me_ai_providers import me_ai_providers_router
from quberi_api.api.v1.me_external_access import me_external_access_router
from quberi_api.api.v1.oauth import oauth_router
from quberi_api.api.v1.agents.router import agents_router
from quberi_api.api.v1.auth.router import auth_router, email_settings_router
from quberi_api.api.v1.conversations.router import (
    conversation_router,
)
from quberi_api.api.v1.conversations.router import (
    space_scoped_router as conversations_space_router,
)
from quberi_api.api.v1.guest_chat import guest_chat_router
from quberi_api.api.v1.integrations.feishu import feishu_router
from quberi_api.api.v1.organizations.ai_providers import ai_providers_router
from quberi_api.api.v1.organizations.audit import recent_events_router
from quberi_api.api.v1.organizations.document_sources import document_sources_router
from quberi_api.api.v1.organizations.groups import groups_router
from quberi_api.api.v1.organizations.members import router as org_members_router
from quberi_api.api.v1.organizations.mine import mine_router
from quberi_api.api.v1.organizations.org_units import org_units_router
from quberi_api.api.v1.organizations.positions import positions_router
from quberi_api.api.v1.organizations.settings import organization_settings_router
from quberi_api.api.v1.platform import router as platform_router
from quberi_api.api.v1.spaces.access import space_access_router
from quberi_api.api.v1.spaces.access_requests import access_requests_router
from quberi_api.api.v1.spaces.document_access import document_access_router
from quberi_api.api.v1.spaces.documents import space_documents_router
from quberi_api.api.v1.account import account_router
from quberi_api.api.v1.spaces.router import router as spaces_router
from quberi_api.api.v1.usage import usage_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
router.include_router(email_settings_router)
router.include_router(platform_router)
router.include_router(admin_auth_router)
router.include_router(admin_organizations_router)
router.include_router(admin_stats_router)
router.include_router(admin_ai_providers_router)
router.include_router(spaces_router)
router.include_router(space_access_router)
router.include_router(org_members_router)
router.include_router(mine_router)
router.include_router(org_units_router)
router.include_router(positions_router)
router.include_router(organization_settings_router)
router.include_router(groups_router)
router.include_router(recent_events_router)
router.include_router(document_sources_router)
router.include_router(ai_providers_router)
router.include_router(feishu_router)
router.include_router(space_documents_router)
router.include_router(document_access_router)
router.include_router(access_requests_router)
router.include_router(conversations_space_router)
router.include_router(conversation_router)
router.include_router(agents_router)
router.include_router(usage_router)
router.include_router(guest_chat_router)
router.include_router(account_router)
router.include_router(me_ai_providers_router)
router.include_router(oauth_router)
router.include_router(external_router)
router.include_router(me_external_access_router)

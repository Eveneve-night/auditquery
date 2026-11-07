"""Configurable chat-completions protocol; no fallback or implicit network access."""
from dataclasses import dataclass
import http.client
import ipaddress
import math
import re
import socket
import ssl
from urllib.parse import urlsplit

from .bailian import BailianProvider, HttpResponse
from .credentials import Credential, CredentialError, _validate_key
from .errors import ProviderError, ProviderErrorCode
from .usage import UsageLedger


def invalid():
    return ProviderError(ProviderErrorCode.CONFIGURATION, "Invalid model configuration or unknown pricing", retryable=False)


def public_address(value):
    ip=ipaddress.ip_address(value)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


@dataclass(frozen=True)
class ServiceConfig:
    provider_id: str
    model: str
    base_url: str
    local: bool
    auth_mode: str
    input_per_million_tokens: float | None
    output_per_million_tokens: float | None
    timeout_seconds: float = 10
    max_completion_tokens_cap: int = 1500
    max_response_bytes: int = 2097152
    require_response_model_match: bool = True

    @property
    def endpoint(self):
        return self.base_url.rstrip('/') + '/chat/completions'

    @classmethod
    def from_dict(cls, data):
        fields = {'version','protocol','provider_id','model','base_url','local','auth_mode',
                  'input_per_million_tokens','output_per_million_tokens'}
        if not isinstance(data, dict) or set(data) != fields:
            raise invalid()
        if data['version'] != '1.0.0' or data['protocol'] != 'openai-chat-completions':
            raise invalid()
        if (not isinstance(data['local'], bool) or data['auth_mode'] not in ('bearer','none')
                or (data['auth_mode']=='none' and not data['local'])):
            raise invalid()
        for name in ('provider_id','model'):
            if not isinstance(data[name],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',data[name]):
                raise invalid()
        for name in ('input_per_million_tokens','output_per_million_tokens'):
            v=data[name]
            if v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0):
                raise invalid()
        url=data['base_url']
        if not isinstance(url,str) or len(url)>2048 or any(ord(c)<33 or ord(c)>126 for c in url):
            raise invalid()
        try:
            u=urlsplit(url)
            if (not u.hostname or u.username is not None or u.password is not None or '?' in url or '#' in url
                    or '%' in u.netloc or '\\' in url or u.port == 0):
                raise invalid()
            if data['local']:
                if u.scheme not in ('http','https') or not (u.hostname=='localhost' or ipaddress.ip_address(u.hostname).is_loopback):
                    raise invalid()
            elif u.scheme!='https' or u.hostname=='localhost':
                raise invalid()
            else:
                try:
                    if not public_address(u.hostname): raise invalid()
                except ValueError: pass
        except (ValueError,TypeError):
            raise invalid() from None
        return cls(**{k:data[k] for k in cls.__dataclass_fields__ if k in data})

    def public(self):
        return dict(version='1.0.0', protocol='openai-chat-completions',
                    **{k:getattr(self,k) for k in ('provider_id','model','base_url','local','auth_mode',
                        'input_per_million_tokens','output_per_million_tokens')})


class PinnedTransport:
    """Resolve/validate every address and connect to a validated IP; no proxy/redirect."""
    def __init__(self, config): self.config=config

    def send(self, *, url, headers, body, timeout_seconds, max_response_bytes):
        if url != self.config.endpoint: raise invalid()
        u=urlsplit(url); port=u.port or (443 if u.scheme=='https' else 80)
        try:
            addresses=socket.getaddrinfo(u.hostname,port,type=socket.SOCK_STREAM)
            ips=[v[4][0] for v in addresses]
            if not ips or any(not (ipaddress.ip_address(ip).is_loopback if self.config.local
                                  else public_address(ip)) for ip in ips):
                raise invalid()
            conn=http.client.HTTPConnection(u.hostname,port,timeout=timeout_seconds)
            sock=socket.create_connection((ips[0],port),timeout=timeout_seconds)
            if u.scheme=='https':
                try: sock=ssl.create_default_context().wrap_socket(sock,server_hostname=u.hostname)
                except BaseException: sock.close(); raise
            conn.sock=sock
            try:
                conn.request('POST',u.path or '/',body=body,headers=headers)
                r=conn.getresponse(); payload=r.read(max_response_bytes+1)
                if len(payload)>max_response_bytes: raise invalid()
                return HttpResponse(r.status,dict(r.getheaders()),payload)
            finally: conn.close()
        except ProviderError: raise
        except (TimeoutError,socket.timeout):
            raise ProviderError(ProviderErrorCode.TIMEOUT,'Model connection timed out',retryable=True) from None
        except (OSError,http.client.HTTPException):
            raise ProviderError(ProviderErrorCode.NETWORK,'Model connection failed',retryable=False) from None


class FixedCredential:
    def __init__(self,value): self.value=value
    def load(self): return Credential(self.value,'encrypted_model_settings')


class OpenAICompatibleProvider(BailianProvider):
    """Reuse one-attempt parsing/accounting, not Bailian's fixed endpoint policy."""
    def __init__(self,config,credential,*,transport=None,usage_ledger=None):
        self.config=ServiceConfig.from_dict(config.public())
        self.provider_id=config.provider_id; self.model=config.model
        if config.auth_mode=='bearer': _validate_key(credential)
        elif credential: raise invalid()
        self.credential_provider=FixedCredential(credential)
        self.transport=transport or PinnedTransport(config)
        self.usage_ledger=usage_ledger or UsageLedger()

    def _protocol_options(self):
        # Preserve the old preset's wire behavior without imposing it on other
        # OpenAI-compatible services or preventing any configured model.
        if (self.config.provider_id=='bailian_beijing' and self.model=='qwen3.7-flash'
                and self.config.base_url.rstrip('/')=='https://dashscope.aliyuncs.com/compatible-mode/v1'):
            return {'enable_thinking':False}
        return {}

    def _parse_success(self, request, response, **kwargs):
        result=super()._parse_success(request,response,**kwargs)
        secret=kwargs['secret']
        def reflected(value):
            if isinstance(value,str):
                # A one-character token must not classify every ordinary word
                # as a secret. Exact-value reflection still fails closed.
                return bool(secret and (secret == value if len(secret)<8 else secret in value))
            if isinstance(value,dict): return any(reflected(k) or reflected(v) for k,v in value.items())
            if isinstance(value,list): return any(reflected(v) for v in value)
            return False
        if reflected(result.parsed_output):
            raise ProviderError(ProviderErrorCode.INVALID_RESPONSE,'Provider reflected credential content',
                retryable=False,input_tokens=result.usage.input_tokens,output_tokens=result.usage.output_tokens,
                total_tokens=result.usage.total_tokens,estimated_cost_cny=result.estimated_cost_cny)
        return result

    @classmethod
    def _request_id(cls, headers):
        # Remote identifiers are not trusted diagnostics (they can echo content).
        return None

    @staticmethod
    def _decode_object(body, *, secret):
        value=BailianProvider._decode_object(body,secret=secret)
        value.pop('request_id',None); value.pop('id',None)
        return value

    @classmethod
    def _http_error(cls,response,*,provider_request_id,secret):
        # Map status/retry metadata, never preserve untrusted error body text.
        return super()._http_error(HttpResponse(response.status,{},b'{}'),
                                   provider_request_id=None,secret=secret)

    def generate_structured(self,request):
        # Unknown cost must not silently bypass the existing currency budget.
        if self.config.input_per_million_tokens is None or self.config.output_per_million_tokens is None:
            raise invalid()
        return super().generate_structured(request)

from pathlib import Path
from pydantic import SecretStr
import pytest
from crashtest.hosted import run_hosted
from crashtest.settings import Settings


@pytest.mark.asyncio
async def test_hosted_f1_is_unavailable_without_creating_session(tmp_path, monkeypatch):
    class Client:
        def __init__(self, key): pass
        async def aclose(self): pass
        async def _request(self, *args, **kwargs):
            raise AssertionError('F1 must not create a hosted session')
    monkeypatch.setattr('crashtest.hosted.AgentsClient', Client)
    settings = Settings(openai_api_key=SecretStr('app'), crashtest_root=tmp_path, _env_file=None)
    result = await run_hosted(settings, fault='F1')
    assert result['status'] == 'NOT_IMPLEMENTED'
    assert (Path(result['run_dir']) / 'report.json').exists()

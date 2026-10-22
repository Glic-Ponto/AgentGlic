# Glic Engineering Agent

Agente Codex especializado no Glic Project. O repositório separa o Brain permanente, o RAG local, as configurações, as Skills e o workspace sem criar subagentes, serviços ou infraestrutura adicional.

O RAG preservado indexa, em modo somente leitura, código e documentação de um único repositório autorizado no GitHub. Ele recupera contexto com origem verificável e não interpreta conteúdo remoto como instrução ou autorização.

## Estrutura

```text
.
├── a_core/
│   ├── brain/
│   │   └── instructions.md
│   ├── config/
│   │   ├── config.yaml
│   │   └── .env.example
│   └── rag/
│       ├── knowledge/
│       │   ├── architecture.md
│       │   ├── business-rules.md
│       │   └── stack.md
│       ├── store/.gitkeep
│       ├── tests/
│       │   ├── test_index.py
│       │   └── test_retrieve.py
│       ├── __init__.py
│       ├── github_client.py
│       ├── index.py
│       ├── retrieve.py
│       └── requirements.txt
├── b_skill/
│   ├── a_front/SKILL.md
│   ├── b_back/SKILL.md
│   ├── c_test/SKILL.md
│   ├── d_secure/SKILL.md
│   └── e_architecture/SKILL.md
├── c_workspace/
│   ├── ws_input/.gitkeep
│   └── ws_output/.gitkeep
├── AGENTS.md
├── .gitignore
└── README.md
```

## Responsabilidades

- `AGENTS.md`: ponto de entrada e roteador explícito do Codex.
- `a_core/brain/`: regras permanentes, sem repetir procedimentos específicos das Skills.
- `a_core/config/`: configuração do projeto, do GitHub e dos caminhos locais.
- `a_core/rag/`: cliente GitHub, indexação, recuperação, conhecimento, testes e armazenamento local.
- `b_skill/`: instruções especializadas de frontend, backend, testes, segurança e arquitetura.
- `c_workspace/ws_input/`: materiais fornecidos ao agente para análise.
- `c_workspace/ws_output/`: relatórios, auditorias e artefatos auxiliares produzidos.

Alterações de frontend, backend ou testes do Glic Project devem ser feitas no repositório original da aplicação, nunca em `ws_output/`.

## Requisitos e instalação

- Python 3.11 ou superior.
- Acesso HTTPS à API do GitHub para indexação remota.
- Token restrito ao repositório autorizado.

Crie e ative um ambiente virtual, depois instale as dependências preservadas do RAG:

```bash
python -m venv .venv
python -m pip install -r a_core/rag/requirements.txt
```

No PowerShell, a ativação é feita com:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Configuração

Edite `a_core/config/config.yaml`:

```yaml
project:
  name: Glic Project
  local_path: null # caminho opcional para um clone local do Glic Project

github:
  owner: "minha-organizacao"
  repo: "glic-project"
  ref: null # null usa a branch padrão
```

`project.local_path` centraliza a localização de um clone local para uso futuro ou inspeção pelo Codex. A implementação atual do indexador continua usando a API do GitHub e não adiciona uma nova estratégia de indexação nesta reorganização.

Defina `GITHUB_TOKEN` somente no ambiente. O exemplo está em `a_core/config/.env.example`; não grave o token em arquivos versionados.

São aceitos tokens de instalação de GitHub App ou fine-grained personal access tokens com acesso apenas ao repositório autorizado e estas permissões:

- Contents: Read-only.
- Metadata: Read.

Exemplo no PowerShell:

```powershell
$env:GITHUB_TOKEN = "seu-token-somente-leitura"
```

## Indexar e consultar

Crie ou atualize o índice:

```bash
python -m a_core.rag.index
```

Consulte contexto fundamentado:

```bash
python -m a_core.rag.retrieve "Como funciona a autenticação?"
python -m a_core.rag.retrieve "validação do formulário de cadastro" --top-k 3
```

O caminho padrão da configuração é `a_core/config/config.yaml`. Use `--config caminho/alternativo.yaml` apenas quando necessário.

Cada resultado informa score, repositório, arquivo, linhas e SHA do commit. Arquivos cujo blob SHA não mudou são reutilizados. Se uma sincronização falhar, o índice é marcado como possivelmente desatualizado.

## RAG e segurança

A implementação usa `feature-hashing-v1`, um embedding lexical local e determinístico. Nenhum trecho é enviado a um provedor de embeddings. Vetores, chunks e metadados ficam em `a_core/rag/store/index.sqlite3`, ignorado pelo Git.

O indexador mantém as proteções existentes:

- allowlist de extensões textuais e limite de tamanho;
- exclusão de dependências, builds, caches e binários;
- exclusão de `.env`, chaves privadas e formatos sensíveis;
- detecção básica de padrões comuns de tokens e credenciais;
- bloqueio de redirects e de acesso a outro repositório.

Essas proteções não substituem secret scanning dedicado no repositório da aplicação.

## Brain e Skills no Codex

O Codex começa por `AGENTS.md`, que o orienta a ler `a_core/brain/instructions.md` e referencia diretamente cada arquivo em `b_skill/`. Essa referência explícita permite carregar as Skills apesar de elas estarem fora do diretório convencional `.agents/skills/`, sem manter cópias duplicadas.

Os nomes YAML das Skills usam o formato válido do Codex (`a-front`, `b-back`, `c-test`, `d-secure` e `e-architecture`); os diretórios mantêm a nomenclatura estrutural solicitada com underscores.

## Conhecimento e workspace

Os documentos em `a_core/rag/knowledge/` contêm apenas conhecimento curado. Informações ainda não confirmadas continuam marcadas como “Pendente de documentação”.

Não armazene credenciais ou cópias desnecessárias da aplicação em `c_workspace/`. Para limpar arquivos gerados, remova o conteúdo de `a_core/rag/store/`, `c_workspace/ws_input/` e `c_workspace/ws_output/`, preservando os respectivos `.gitkeep`.

## Testes

Os testes usam fixtures e mocks, sem credenciais ou chamadas externas:

```bash
python -m pytest -q a_core/rag/tests
```

## Limitações iniciais

- A integração GitHub permanece estritamente somente leitura.
- `project.local_path` apenas centraliza a configuração; o RAG existente continua indexando pela API remota.
- O embedding local é lexical e menor que modelos semânticos hospedados.
- A árvore recursiva é interrompida se o GitHub sinalizar truncamento.
- A integração remota ponta a ponta exige `owner`, `repo` e token autorizados.

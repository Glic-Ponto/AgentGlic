# Glic Engineering Agent

Este arquivo é o ponto de entrada do Codex neste repositório.

Antes de executar uma tarefa:

1. Leia `a_core/brain/instructions.md`.
2. Consulte `a_core/config/config.yaml` e o conhecimento em `a_core/rag/knowledge/` quando a tarefa depender do contexto do Glic Project.
3. Carregue somente as Skills pertinentes:
   - frontend: `b_skill/a_front/SKILL.md`
   - backend: `b_skill/b_back/SKILL.md`
   - testes e qualidade: `b_skill/c_test/SKILL.md`
   - segurança: `b_skill/d_secure/SKILL.md`
   - arquitetura: `b_skill/e_architecture/SKILL.md`

Os caminhos explícitos acima são o mecanismo de descoberta das Skills fora do diretório convencional `.agents/skills/`. Siga o Brain e as Skills selecionadas sem duplicar suas instruções aqui.

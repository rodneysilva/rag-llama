# Política de segurança

## Modelo de ameaça (resumo)

O RagAroy roda LLM local e dados do usuário; a superfície exposta é a API web
(login por perfis) e, opcionalmente, túneis para os serviços de GPU.

- **Autenticação**: contas com hash scrypt (`users.json`, fora do git) + tokens
  de sessão HMAC; login com rate limit; perfis isolam sessões/conversas por
  dono. Configurações, troca de modelo e MCP são exclusivos do admin.
- **Sandbox de execução**: container próprio sem portas publicadas (só rede
  interna do compose), usuário não-root, arquivos de teste efêmeros (limpos ao
  fechar o modal). O preview público de arquivos do teste exige token HMAC
  curto (15 min).
- **Ferramentas MCP**: todo registro/instalação é admin-only; **toda execução
  de ferramenta durante o chat passa por aprovação explícita do usuário** e a
  resposta final é verificada contra o registro real das ferramentas.
- **Segredos**: nada sensível no repositório — `.env` (chaves de API, tokens,
  admin), `users.json` (hashes) e diretórios de estado (`sessions/`, `saidas/`,
  `logs/`, `datasets/`, volumes) são gitignored. O CI usa apenas secrets do
  GitHub.

## O que NUNCA commitar

`.env`, `users.json`, `sessions/`, `saidas/`, `logs/`, `qdrant_data/`,
`rabbit_data/`, `hf_cache/`, `modelos_voz/`, `datasets/` (conteúdo pessoal).
Ao aportar um arquivo novo de estado, adicione ao `.gitignore` na mesma PR.

## Reportar vulnerabilidade

Abra uma issue privada de segurança no repositório (GitHub → Security →
"Report a vulnerability") ou contate o mantenedor diretamente. Não abra issue
pública com detalhes exploráveis.

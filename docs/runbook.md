# Como fazer a troca e voltar

[English](runbook.en.md) · [Início](../README.md)

Os lados são `a` (`kind-devops-notes`) e `b` (`kind-devops-recovery`). Só o namespace `failover-demo` é gerenciado. Os namespaces dos labs anteriores não fazem parte da troca. O código gera os objetos Kubernetes em `scripts/lab.py`; imagens e configurações relevantes ficam no Git, senhas ficam em `.local` e Secrets.

## Antes de começar

Confira `python3 scripts/lab.py status`, espaço livre no Docker e o acesso aos dois clusters. O banco ativo deve estar fora de recovery; o outro deve estar em recovery e aparecer como `sync` em `pg_stat_replication`. Não use `init` para recuperar um ambiente existente: ele recusa a operação para não substituir dados ou credenciais.

Mantenha uma cópia privada de `.local/credentials.json` e `.local/state.json` em armazenamento apropriado. Eles não substituem um backup do banco. Este lab mantém dados em PVCs locais dos nós kind; remover o cluster também remove esses dados. O roteiro não destrói nenhum cluster.

## A ida e a volta

1. Acesse localhost:8023 e crie uma nota. Confira a réplica.
2. Execute `python3 scripts/lab.py switch`. O mesmo comando serve para failover e failback.
3. O script fecha a rota, para a API antiga e espera seus Pods terminarem. Lê uma barreira de WAL e espera a réplica aplicá-la.
4. Escala o banco anterior para zero, espera os Pods sumirem e consulta o runtime de **todos** os nós kind desse cluster. Se ainda existir container desse namespace, ou algum nó não puder ser consultado, recusa a promoção.
5. Promove a réplica. A API continua parada. Cria um PVC novo na origem e executa `pg_basebackup` a partir do banco promovido, sem montar nem apagar o volume antigo.
6. Ativa novamente a replicação síncrona, inicia a API no lado novo e abre a rota.
7. Crie uma nota nesse lado e repita a troca para voltar. Confira dados, IDs e timestamps. Sequências podem ter saltos após promoção/reinício; o teste exige que não recuem nem reutilizem IDs existentes.

O gateway responde 503 enquanto a troca está registrada em andamento; uma indisponibilidade do upstream fora desse intervalo gera 502. Uma requisição já em andamento pode perder a resposta. Não repita uma escrita sem conferir o resultado. Os NodePorts e o acesso administrativo são ferramentas do lab; não tente escrever diretamente no banco durante a promoção.

## Se o processo parar no meio

O arquivo `.local/state.json` registra fases e o volume novo antes de tentar usá-lo. A rota fica fechada enquanto a fase não for `ready`. O lock impede dois comandos do lab ao mesmo tempo. Não há supervisor externo para retomar automaticamente.

Inspecione Pods, logs do banco e `.local/state.json`, corrija o problema de acesso, espaço ou autenticação e execute:

```bash
python3 scripts/lab.py resume
```

`resume` continua a fase registrada: drena, confirma o bloqueio, promove quando ainda necessário ou termina de reconstruir a réplica. Ele não volta a promover um banco já promovido. Um clone incompleto é recusado: mantenha o PVC com problema para análise e use um PVC vazio novo, atualizando a referência pendente somente depois de conferir qual banco é o primário. Não apague `PGDATA` nem force o uso de uma cópia antiga.

Se o arquivo de estado estiver perdido ou a situação não corresponder ao registro, pare o gateway e mantenha as APIs em zero. Identifique o primário e a timeline com acesso administrativo aos dois lados e restaure o registro privado antes de automatizar outra troca. Não há comando de promoção cega para cluster inacessível.

O teste também usa lock. Se falhar, consulte `evidence/verification.json` antes de repetir: ele pode ter terminado com o lado `b` ativo. Volte ao lado `a` usando a troca normal ou retome a operação interrompida. O bloco do teste que para a réplica tenta ligá-la novamente mesmo se a requisição falhar.

## Guardar e remover recursos

Cada troca retém o PVC anterior e cria outro de 1 GiB. Isso ocupa espaço. Liste os PVCs e compare com `volumes` e `history` no estado. Não remova os dois volumes atuais. Uma cópia antiga não deve voltar como primário depois de novas escritas no outro lado: ela precisa ser reconstruída como réplica.

Não há limpeza destrutiva automática no computador local. Para encerrar a demonstração sem perder os volumes, pare o gateway e escale a API para zero nos dois lados. Uma pausa do banco ativo deve ser coordenada com a réplica; commits pendentes podem ficar esperando. Para desmontar definitivamente, exporte e teste um backup antes de remover o namespace. Não use os comandos de limpeza do CI nos clusters locais.

## Limites de rede e acesso

Os bancos usam SCRAM e a aplicação mantém um usuário sem superpoderes. O papel de replicação é separado; a API recebe só o arquivo de senha dela. O pgpass fica com modo 0600 no volume do banco. Containers rodam sem root ou token de ServiceAccount; o controlador local usa seus kubeconfigs.

A replicação deste exercício não usa TLS e a regra de autenticação aceita o papel de replicação de qualquer endereço alcançável. Os NodePorts ficam acessíveis na rede dos nós. Use apenas o ambiente Docker local de estudo. Para produção, seriam necessários rede restrita, TLS, gestão de segredos, mecanismo externo de fencing/quorum, backups e exercícios em domínios de falha independentes.

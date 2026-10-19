# O que aconteceu nos testes

[English](validation.en.md) · [Procedimento](runbook.md)

A execução local usou os clusters kind já existentes, com um namespace `failover-demo` novo em cada um. A aplicação e os bancos dos labs anteriores não foram usados como destino da replicação. O teste compara seus workloads, imagens e UIDs dos PVCs antes e depois; não precisa alterar nem mesclar aqueles dados.

O roteiro cria notas por HTTP, confirma a réplica síncrona e provoca a indisponibilidade da API. Depois fecha a rota, drena a aplicação, espera uma barreira de WAL, para o banco antigo e confere o runtime dos nós antes da promoção. O cluster de recuperação recebe outra nota. O failback carrega essa nota de volta e reconstrói a réplica no outro lado. No final, o Pod do primário é substituído e os dados são conferidos novamente.

Há também uma interrupção planejada do controlador antes da promoção no failback: uma exceção interrompe o procedimento depois de parar o escritor. O teste exige resposta 503 no gateway e ausência de containers antigos em todos os nós. `resume` termina a operação a partir do estado gravado. Isso testa uma falha do processo de controle, não uma queda da máquina.

## Uma escrita com resultado incerto

O teste para a réplica e envia um POST real. O gateway não recebe a resposta do upstream dentro de dez segundos e devolve 502. O PostgreSQL já pode ter confirmado o commit local e estar esperando a réplica. O teste não repete o POST: religa a réplica e encontra a nota uma única vez nos dois lados. A indisponibilidade aparece nos logs e no relatório, sem transformar a falha em sucesso HTTP.

Na primeira tentativa desse teste, eu havia usado `statement_timeout` para interromper o commit. A espera por replicação síncrona não terminou como eu esperava e o comando externo venceu seu limite de 30 segundos. A réplica foi religada e a escrita reconciliada. Troquei essa suposição pelo teste HTTP acima, que representa a experiência do cliente e não promete rollback.

## A sequência não precisa ser contínua

A primeira troca preservou linhas e timestamps, mas uma verificação exigia que `last_value` da sequência fosse idêntico antes e depois da promoção. Isso falhou. O PostgreSQL pode adiantar a sequência durante recuperação; um salto não significa uma nota perdida. O [relatório dessa suposição incorreta](../evidence/sequence-assumption-failure.json) foi mantido.

A verificação atual exige preservação das linhas e dos IDs existentes, sequência sem retrocesso e uma nova escrita com ID posterior aos anteriores. Ela não altera a sequência para deixar os números bonitos. A [documentação do PostgreSQL sobre sequências](https://www.postgresql.org/docs/17/functions-sequence.html) explica por que valores sem saltos não são garantidos.

## Resultado local de 19/10/2026

Passaram **48 verificações** em **106,98 segundos**. O failover preservou 12 notas e levou 28,63 segundos a partir da chamada de troca; o failback preservou 13 e levou 31,04 segundos. A escrita final deixou 14 notas nos dois bancos. Esses totais incluem notas mantidas das tentativas anteriores.

O estado final tem o lado `a` ativo, `b` como réplica síncrona e HTTP 200 no endereço da aplicação. Quatro volumes antigos ficaram retidos ao longo das tentativas e da execução final. Nenhum workload, imagem ou UID de PVC dos outros labs mudou.

## Como ler as evidências

[verification.json](../evidence/verification.json) traz cada verificação, a quantidade de linhas em cada troca, hashes das linhas completas, barreiras de WAL, nós consultados para confirmar o bloqueio e os volumes retidos. Valores reais das notas e credenciais ficam em arquivos privados. Os tempos começam na chamada do procedimento de troca; não incluem a criação inicial dos clusters nem equivalem a uma medição contínua de indisponibilidade.

O GitHub Actions repete a inicialização e o mesmo roteiro em dois clusters novos. A [execução aprovada do CI](https://github.com/tommmdl/failover-lab/actions/runs/39310992536) passou nas mesmas **48 verificações**, em 75,13 segundos de teste. O failover levou 20,20 segundos e preservou 4 notas; o failback levou 18,48 segundos e preservou 5. A escrita final deixou 6 notas em cada banco. O [relatório baixado do artefato](../evidence/ci-verification.json) e os [metadados da execução](../evidence/ci-run.json) ficam junto das evidências locais. A primeira execução do CI parou antes de criar clusters por um endereço incorreto de checksum do kind; o endereço foi corrigido para o arquivo `.sha256sum` publicado no release. Os logs de diagnóstico não incluem Secrets nem dumps.

Esses resultados demonstram o procedimento controlado deste ambiente. Não medem falha do host, perda de disco, isolamento completo de um cluster, throughput de produção ou eleição automática. Não há RTO de produção nem garantia geral de RPO zero para falhas que não foram testadas.

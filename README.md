# JobScout

Agent personnel de veille d'offres d'emploi : il collecte des offres sur plusieurs sources, les **score contre mon profil** (mots-clés pondérés, localisations, exclusions), déduplique ce qu'il a déjà vu, et **m'alerte sur Telegram** avec le lien pour postuler — en option, une lecture IA classe les meilleures offres et propose un angle d'attaque de candidature.

C'est la même grammaire d'agent que mes autres projets ([prime-engine](https://github.com/Sb2z/prime-engine), [controldone](https://github.com/Sb2z/controldone)) : **déclencheur → collecte → scoring → état → alerte**, en un seul fichier Python sans dépendance lourde, pensé pour être piloté par un orchestrateur.

```
tâche planifiée / n8n / Power Automate  (déclencheur)
   → collecte      Remotive · Arbeitnow · Adzuna FR/CH (si clé)
   → scoring       profile.yaml : mots-clés pondérés (titre x3, tags x2, texte x1),
                    bonus localisation, exclusions, seuil
   → dédup         data/seen.json — seules les NOUVELLES offres alertent
   → enrichissement (optionnel) Claude classe le top et suggère l'angle de candidature
   → alerte        Telegram (lien direct pour postuler) · --json pour les machines
```

## Utilisation

```bash
pip install -r requirements.txt
cp .env.example .env          # bot Telegram, clés optionnelles

python jobscout.py scan                 # tableau console
python jobscout.py scan --notify        # alerte Telegram des nouvelles offres
python jobscout.py scan --json          # sortie machine-readable (orchestrateurs)
python jobscout.py reset                # oublie l'historique
```

Le **profil est un YAML lisible** (`profile.yaml`) : requêtes de recherche, mots-clés indispensables, pondérations, bonus de lieu, exclusions, seuil. Adapter le profil = adapter l'agent, sans toucher au code.

## Intégration orchestrateur

- **Code retour = nombre de nouvelles offres** (0 = rien de neuf) : une tâche planifiée ou un nœud n8n peut brancher directement dessus.
- `--json` écrit sur stdout un objet stable : `{scanned, matches, new, results:[{score, title, company, url, reasons...}], llm_brief}`.
- Recette type : *Cron (n8n) → Execute Command `python jobscout.py scan --json` → IF `$.new > 0` → Telegram/Teams + création d'une tâche « postuler »*.

## Feuille de route

- Sources supplémentaires (flux RSS d'entreprises cibles, API JSearch) ;
- Suivi de candidatures (offre → postulé → relance) dans le même état local ;
- Brouillon de candidature automatique : lecture de l'offre + génération d'une lettre ciblée via LLM, dépôt en brouillon Gmail.

## Notes

- Sources sans clé : [Remotive](https://remotive.com) (remote) et [Arbeitnow](https://arbeitnow.com) (Europe). [Adzuna](https://developer.adzuna.com) (France/Suisse) s'active avec une clé gratuite dans `.env`. Chaque source est optionnelle : une panne n'arrête jamais le scan.
- Aucune donnée personnelle dans le dépôt : le profil ne contient que des mots-clés de recherche ; l'état et les secrets sont ignorés par git.

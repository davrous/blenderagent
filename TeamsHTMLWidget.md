# Teams HTML Widget : viewer de scene Babylon.js

## Objectif et perimetre

Afficher un instantane GLB de la scene Blender dans un Widget HTML Teams, avec
rotation, zoom et deplacement de la camera.

**Il ne faut pas reutiliser ni embarquer tout le Web Chat.** Le widget doit etre
une interface autonome, concue pour Teams. Il doit uniquement s'inspirer de la
logique du player WebGL Babylon.js existant afin d'obtenir un rendu comparable
a celui du Web Chat.

La reference est [BabylonViewer.tsx](webchat/client/src/components/BabylonViewer.tsx) :
chargement du GLB, configuration du moteur et de la scene, eclairage
d'environnement, camera orbitale, cadrage, controles, redimensionnement et
liberation des ressources. Le composant React, sa modale et son interface ne
doivent pas etre repris tels quels.

Hors perimetre initial :

- Le shell du Web Chat, son historique, son store et ses composants de chat.
- La reutilisation de ses routes relatives `/api/blob` et `/api/assets`.
- La voix, les connexions temps reel et l'edition de la scene dans le widget.
- Un viewport Blender distant ou un rendu identique a Blender/Cycles.
- L'exposition generique des outils Blender ou des actions payantes.
- Un export GLB automatique apres chaque message.

Le MVP est un viewer en lecture seule, propose lorsqu'un export GLB est
disponible. Une future actualisation devra distinguer le renouvellement du lien
d'un instantane existant de la creation d'un nouvel export Blender.

## Etat de faisabilite au 9 novembre 2026

L'architecture est compatible en principe avec notre hosted agent utilisant
l'Activity Protocol et Microsoft 365 Agents SDK. Aucune migration vers le Teams
SDK TypeScript ni aucun nouveau serveur MCP ne sont requis pour un widget
appartenant au bot.

Verifications locales effectuees :

- Le SDK Python accepte et conserve `text_format="extendedmarkdown"`.
- L'application permet d'enregistrer une route Activity dediee.
- L'adaptateur HTTP du host conserve le corps JSON d'une reponse `invoke`.
- WidgetBot utilise des bundles React/JavaScript et CSS externes : le widget
  n'est pas limite a du JavaScript inline.

Points non encore verifies dans Teams :

- La creation effective d'un contexte WebGL dans l'iframe du widget.
- Le transport de `htmlwidget/calltool` et de sa reponse via la passerelle Foundry.
- La disponibilite de la preview pour le tenant, les clients et les conversations
  vises, notamment les threads de channel.
- La necessite eventuelle de l'endpoint Bot API Canary utilise par WidgetBot.

Ni les ressources consultees ni l'exemple WidgetBot ne constituent une preuve
du fonctionnement de Babylon.js/WebGL dans Teams. Ces points sont des portes
de validation du POC, pas des fonctionnalites deja implementees.

## Protocole Widget

### Envoi du message

Le bot envoie une activite `message` avec `textFormat: "extendedmarkdown"`.
Le texte contient un bloc de code `html-widget`, dont le contenu est un objet
JSON comprenant :

- `type: "widget/mcp-ui"`.
- `name` et, facultativement, `description`.
- `domain`, une origine HTTPS associee au widget.
- `html`, le document HTML complet.
- `securityPolicy`, les origines explicitement autorisees.
- Un `correlationId` pour retrouver le contexte serveur du widget.

Le nom `widget/mcp-ui` n'impose pas de serveur MCP. Le parcours des widgets
produits par un serveur MCP est distinct et n'est pas necessaire au MVP.

### Communication avec Teams et le bot

1. Le widget effectue le handshake JSON-RPC `ui/initialize`, puis
   `ui/notifications/initialized`.
2. Il rapporte sa hauteur avec `ui/notifications/size-changed`.
3. Une action utilisateur peut declencher `tools/call`.
4. Teams transmet au bot une activite de type `invoke`, nommee
   `htmlwidget/calltool`, avec `value.name` et `value.arguments`.
5. Le bot retourne un corps `responseType: "htmlwidget/calltoolresult"` contenant
   `callToolResult`, avec les trois champs `content`, `structuredContent` et
   `isError` toujours renseignes.

Dans Microsoft 365 Agents SDK, cette reponse utilise une activite
`invokeResponse` avec `value.status` et `value.body`. La route doit selectionner
precisement le type et le nom de l'invoke, sans intercepter les autres invokes.

Les appels Widget restent dans l'Activity Protocol HTTP. Ils ne doivent pas etre
confondus avec le protocole `invocations_ws` utilise pour la voix.

## Architecture recommandee

```text
Blender -> export GLB -> Blob prive
                           |
Agent -> message Widget -> Teams -> viewer Babylon.js autonome

Clic "Charger la scene"
  -> tools/call -> Teams -> invoke Activity -> agent
  -> validation du contexte -> URL temporaire de lecture
  -> telechargement du GLB -> rendu local dans le navigateur
```

Le rendu et les mouvements de camera sont locaux au navigateur. Ils ne doivent
pas executer un tour LLM ni maintenir artificiellement le hosted agent eveille.
Une requete d'actualisation peut subir un cold start : afficher une erreur
recuperable et permettre de reessayer.

### Integration au backend

- Dans [l'export GLB](main.py), enregistrer une reference serveur a l'artefact :
  identifiant Blob, version de l'instantane, conversation et generation de scene.
  Ne pas deduire les droits d'acces d'une URL produite par le modele.
- Dans [activity_bridge.py](activity_bridge.py), publier le widget lorsqu'un
  export fiable est disponible et ajouter son handler `invoke`.
- Envoyer le widget dans une activite complete distincte des statuts. Ne pas
  streamer son HTML par fragments ni modifier le correctif des threads.
- Reutiliser [ArtifactStorage](artifact_storage.py) pour les liens temporaires
  de lecture et les mecanismes de persistance adaptes.
- Conserver le contexte du widget au-dela d'un redemarrage du conteneur :
  un ancien message Teams peut encore etre ouvert et appeler le bot.
- Retourner rapidement les metadonnees d'un export deja disponible. Ne pas
  lancer un rendu ou un export long dans l'invoke de chargement.

Preserver les autres parcours : Responses, Activity, voix, liens de
telechargement, statuts et controles d'autorisation des jobs video.

### Frontend autonome

- Creer une entree frontend et un bundle specifiques au widget.
- S'inspirer uniquement des choix de rendu Babylon.js du player existant.
- Adapter l'interface aux themes et dimensions Teams.
- Heberger le JavaScript, le CSS et les ressources d'environnement sur des URL
  HTTPS accessibles, avec des versions stables.
- Garder un bootstrap HTML leger et deterministe, produit par le serveur,
  sans HTML executable genere par le modele.
- Prevoir chargement explicite, erreurs recuperables, reinitialisation de la
  camera et lien de telechargement ou de repli.

## Ressources, CSP et CORS

Le widget s'execute sur une origine generee par Teams, pas sur l'origine du Web
Chat ni sur celle declaree dans `domain`.

Pour les widgets propres au bot, la documentation decrit une origine de la forme
`https://<hash>.widget-renderer.usercontent.microsoft`.
Le texte a hacher est le **MRI Teams complet en minuscules**,
`28:<bot-id>`, et non le GUID seul. Le hash est constitue des 32 premiers
caracteres hexadecimaux du SHA-256 de ce MRI. Entrer ce MRI dans le
[widget domain tool](https://widgettool.z19.web.core.windows.net/), puis
verifier l'origine effective dans le client Teams.

- `resourceDomains` autorise les origines des scripts, styles, images et polices.
- `connectDomains` autorise les origines contactees par `fetch`, XHR ou WebSocket.
- Le service servant le GLB doit accepter l'origine effective du widget via CORS.
- Les modules JavaScript externes doivent egalement etre servis avec les
  en-tetes CORS appropries.
- Limiter les autorisations aux origines necessaires. Une autorisation CORS
  n'est pas un mecanisme d'authentification.

Les chemins relatifs du
[proxy Web Chat](webchat/server/src/index.ts) ne sont pas reutilisables tels quels.
Ajouter une route statique au conteneur Foundry ne garantit pas non plus son
exposition publique par la passerelle.

Ajouter un domaine au manifest ne remplace ni `securityPolicy` ni CORS.
Choisir l'hebergement statique et les origines avant d'ajuster le manifest.

## Securite et cycle de vie

- Ne placer ni secret, ni token, ni SAS dans le HTML ou les props du message.
  Les personnes pouvant voir le message peuvent lire son HTML.
- Obtenir une URL temporaire de lecture a l'execution, apres validation du contexte.
- Associer le `correlationId` a la conversation/thread, a l'artefact et a la
  generation de scene. Un identifiant de correlation n'est pas une preuve d'identite.
- Refuser les references inconnues, celles d'une autre conversation et celles
  invalidees par `/clear`.
- Ne pas autoriser une operation sur la seule base de `activity.from` : les
  invokes Widget ne preservent pas de facon fiable l'identite Entra utilisateur.
- Limiter le MVP a la lecture dans le contexte conversationnel autorise.
  Les mutations ou besoins d'identite individuelle exigeraient une authentification
  explicite et des controles supplementaires.
- Ne pas activer un parcours de messages prives cibles sans isolation adaptee
  des scenes et de l'historique.
- Traiter les arguments comme non fiables, ignorer les proprietes inconnues
  telles que les metadonnees reservees Teams, et journaliser les erreurs.
- Retourner des erreurs explicites au widget, y compris en cas de lien expire,
  d'artefact supprime ou de contexte invalide.
- Versionner les assets et le contrat du widget : le HTML et la politique d'un
  message deja envoye ne sont pas automatiquement mis a jour par un redeploiement.

## UX et performances

- Proposer une experience ciblee de visualisation, pas une application complete.
- Utiliser un canvas responsive et un dimensionnement pilote par le contenu,
  sans `100vh` ni hauteur a 100 % sur la racine.
- Rapporter la hauteur exacte de la racine via `ResizeObserver`, sans arrondi.
- Fournir des controles clavier, des libelles accessibles, des etats de
  chargement et des erreurs annonces aux technologies d'assistance.
- Respecter les themes Teams, le contraste et les preferences de mouvement reduit.
- Suspendre le rendu lorsqu'il est inactif et liberer moteur, scene et ecouteurs.
- Tester les scenes lourdes et les pertes de contexte WebGL.
- N'ajouter une vue agrandie que lorsque l'API d'hote correspondante est
  confirmee ; conserver un repli vers une experience externe si necessaire.

## Contraintes de preview et manifest

Le [manifest actuel](m365sideloadmanifest/manifest.json) utilise `devPreview`.
Aucun flag Widget dedie n'a ete trouve dans le schema consulte.
`supportsTargetedMessages` ne constitue pas un commutateur d'activation des Widgets.

WidgetBot redirige ses appels Bot API vers `canary.botapi.skype.com`. Clarifier
avec Microsoft si cela est requis par la preview ou propre a cet exemple.
Ne pas recopier cette redirection globalement dans notre agent sans validation
de sa necessite et de sa compatibilite avec Foundry.

La documentation indique aussi que le contenu HTML des Widgets n'est pas
actuellement exportable via les API d'export. Tenir compte des politiques du tenant.

## POC et criteres de validation

1. **Transport minimal** : envoyer un widget simple par notre agent Foundry et
   verifier un invoke de type "ping", avec reception de son resultat dans le widget.
2. **WebGL minimal** : afficher un cube Babylon.js dans Teams et verifier le
   contexte WebGL, le chargement du bundle, les controles et le redimensionnement.
3. **Scene reelle** : charger un GLB prive exporte par notre agent et son
   environnement, sans dependance aux routes du Web Chat.
4. **Robustesse** : verifier expiration et renouvellement du lien, erreurs CORS,
   artefact indisponible, redemarrage/cold start et perte de contexte WebGL.
5. **Isolation** : verifier rejet d'une autre conversation et d'un ancien widget
   apres `/clear`, sans exposition de fonctionnalites payantes.
6. **Teams** : tester en personnel, groupe et thread de channel, notamment deux
   tours successifs. Confirmer les clients et politiques de tenant supportes.
7. **Non-regression** : verifier que les statuts, rendus, telechargements,
   historiques et parcours existants restent fonctionnels.

Ne pas declarer la fonctionnalite validee uniquement sur la base d'un test
navigateur classique ou de la serialisation locale du SDK. La validation finale
doit traverser Teams, Foundry et notre Activity Protocol.

## Sources examinees

- Exports MHTML fournis : Send HTML Widgets, Serve MCP-generated HTML widgets,
  User Experience Guidelines for HTML Widgets.
- Archive WidgetBot fournie : envoi de messages, bridge Widget, assets heberges,
  configuration de build, manifest et particularites Canary.
- [MicrosoftDocs/msteams-docs#14708](https://github.com/MicrosoftDocs/msteams-docs/pull/14708) :
  sources publiques de la documentation preview, dont les recommandations
  d'accessibilite et de format des messages.

Le depot widgetsmith reste non consulte, faute d'acces authentifie.

## Implementation du MVP

Le MVP est maintenant implemente, mais **la validation de la preview dans Teams
via Foundry reste a effectuer**. Les tests de navigateur ne valident pas cette
passerelle ni les politiques du tenant.

### Composants

- [teams_widgets.py](teams_widgets.py) : capture des exports fiables, contrat
  Widget, verification du contexte et renouvellement des liens de lecture.
- [activity_bridge.py](activity_bridge.py) : registre conversationnel durable,
  route exacte `invoke` + `htmlwidget/calltool`, publication apres la reponse
  native finale et invalidation par `/clear`.
- [Viewer autonome](webchat/client/widget/) : JavaScript et CSS sans React ni
  interface de chat, camera orbitale, cadrage, environnement HDR existant,
  controles clavier, themes, hauteur exacte, erreurs et liberation des ressources.
- [Route statique](webchat/server/src/teamsWidgetAssets.ts) :
  `/teams-widget/v1/`, sans acces aux scenes et sans nouveau proxy GLB.

Le registre est dans la ConversationState existante, persistee sur `$HOME` comme
l'historique Activity. Il conserve les **32 derniers widgets** de la conversation.
Le cache de conversation evite qu'un tour deja en attente ecrase un registre plus
recent avec son ancien snapshot d'etat. Le JSON ne contient aucun SAS : seulement
le Blob, son compte, sa taille, son ETag, sa version et les associations de contexte.
La durabilite exige le volume `$HOME` du host ; le repli MemoryStorage local ne
garantit pas la reouverture d'anciens widgets apres redemarrage.

Seul `upload_glb_to_blob` peut enregistrer un export dans le contexte du tour.
Une URL ecrite par le modele n'enregistre aucun widget. Le dernier export du tour
donne un widget ; il n'y a pas d'export supplementaire ou automatique.

La reponse habituelle, ses images et son lien GLB restent inchanges. Le widget
arrive ensuite dans une **activite distincte**, `extendedmarkdown`, jamais dans
un flux natif ouvert. Les pastilles conservent donc leur cycle actuel.

### Activation et deploiement

1. Construire depuis [webchat/](webchat/) avec `npm run build`. Le build genere
   `client/dist/teams-widget/v1/widget.js`, `widget.css` et
   `studio_small_09_1k.hdr`. Les ressources n'importent pas le Web Chat.
2. Publier ces trois fichiers derriere une origine HTTPS publique accessible aux
   clients Teams. La route `/teams-widget/v1/` doit les servir sans connexion
   utilisateur. Le serveur Web Chat existant peut les servir, ou utiliser
   l'hebergement statique dedie decrit ci-dessous. Ces assets sont publics ; pas les GLB.
3. Sur **le serveur Web Chat**, definir `TEAMS_WIDGET_RENDERER_ORIGIN` avec l'origine
   exacte fournie par le widget domain tool pour le **MRI** du bot. Pour le bot
   actuel, utiliser `28:27d67f5b-b7cc-4935-b979-0fdb47ccf365`, ce qui donne
   `https://ecf78bca578133f43cc5a936854c832d.widget-renderer.usercontent.microsoft`.
   Ne pas hacher le GUID sans `28:`. La route n'autorise pas de wildcard
   CORS ni d'autres origines applicatives. Pour Static Web Apps, cette variable
   est lue lors du packaging et devient l'en-tete CORS du site, pas une variable
   d'une API ou d'un serveur Node.
4. Sur le service **Blob** du compte des artefacts, ajouter une regle CORS
   conservant les regles existantes : cette meme origine exacte, methodes `GET`,
   `HEAD`, `OPTIONS`, en-tetes `Range`, exposes `Content-Length`, `Content-Type`,
   `ETag`. Ne pas rendre le conteneur `screenshots` public.
5. Sur **l'agent**, definir `TEAMS_WIDGET_ASSET_BASE_URL` avec l'URL HTTPS
   publique complete de `/teams-widget/v1`. Le champ existe dans
   [agent.yaml](agent.yaml) pointe sur l'installation de ce depot. Une valeur vide
   desactive la fonctionnalite. Pour une execution locale, utiliser la variable
   correspondante dans `.env`.
6. Reconstruire/redeployer le conteneur de l'agent : le Dockerfile embarque le
   module et le bootstrap HTML. Deployer aussi le build des assets et le serveur
   qui les sert, ou le package Static Web Apps. Deployer uniquement l'agent ne
   publie pas les assets.
7. Conserver le manifest `devPreview` existant. Aucun flag Widget invente ni
   changement global vers Bot API Canary n'est ajoute. Si la politique Teams
   exige une entree `validDomains`, ajouter le hostname HTTPS choisi au package ;
   cela ne remplace ni CSP ni CORS.
8. Demander explicitement un export GLB dans Teams, puis tester d'abord
   **Check connection**, ensuite **Load scene**, en personnel, groupe et thread.
   Deux exports successifs doivent produire deux instantanes independants.

Sans URL d'assets, la fonctionnalite est desactivee : les parcours actuels
restent identiques. Une mauvaise configuration est journalisee, sans interrompre
la reponse habituelle. Les assets sont versionnes par contrat : conserver `v1`
pour les messages existants et utiliser un nouveau repertoire en cas de changement
incompatible du protocole ou du bootstrap.

### Installation Azure de ce depot

Les assets ont ete publies le 10 novembre 2026 dans une **Azure Static Web App
Standard** dediee, sans interface de chat, API, identite Foundry, secrets ni GLB :

| Parametre | Valeur |
|---|---|
| Subscription | `480af19d-6138-460b-962c-e2a0b5aca925` (`Private Test Sub DAVROUS`) |
| Resource group | `davrousai-rg` |
| Foundry existant | `davrousfoundry` / `proj-dav`, `northcentralus` |
| Static Web App | `blender-teams-widget`, `eastus2`, SKU `Standard` |
| Agent publie | `blender-agent`, version `18`, statut `active` |
| URL d'assets de l'agent | `https://gentle-smoke-0841c8c0f.3.azurestaticapps.net/teams-widget/v1` |
| Sante de l'hebergement | `https://gentle-smoke-0841c8c0f.3.azurestaticapps.net/health` |
| Stockage prive des GLB | `msdavrous`, conteneur `screenshots`, acces anonyme desactive |

Ce choix remplace uniquement l'hebergement HTTP des fichiers. Les tentatives
App Service B1 (`northcentralus`, `eastus2`), P0v3 et F1 ont ete refusees avec un
quota effectif de zero VM. Aucun plan App Service n'a ete cree. Static Web Apps
n'utilise pas ces VM ; le SKU Standard est facturable.

Pour republier les assets depuis la racine du depot :

```powershell
npm --prefix .\webchat run build
$env:TEAMS_WIDGET_RENDERER_ORIGIN = "https://ecf78bca578133f43cc5a936854c832d.widget-renderer.usercontent.microsoft"
$output = Join-Path $env:TEMP ("blender-teams-widget-" + [Guid]::NewGuid().ToString("N"))
node .\devTools\package_teams_widget.mjs $output
$env:SWA_CLI_DEPLOYMENT_TOKEN = az staticwebapp secrets list `
  --name blender-teams-widget --resource-group davrousai-rg `
  --subscription 480af19d-6138-460b-962c-e2a0b5aca925 `
  --query properties.apiKey -o tsv
swa deploy $output --env production --no-use-keychain
Remove-Item Env:\SWA_CLI_DEPLOYMENT_TOKEN
```

Le dossier de sortie doit etre vide. **Ne pas executer `swa deploy` depuis ce
dossier ou un de ses sous-dossiers** : le client natif le refuse. La version
2.0.6 du wrapper peut alors retourner zero malgre l'echec ; verifier
systematiquement `/health` et les trois fichiers apres deploiement.

Le packager genere seulement les assets `v1`, une page d'information, la sante
et `staticwebapp.config.json`. Les en-tetes autorisent exclusivement l'origine
du renderer, avec un type explicite pour `.hdr`. Il n'y a pas de serveur Node
ou d'API dont il faudrait configurer `AGENT_FOUNDRY_URL`, un secret ou une
identite managee. Le bootstrap HTML reste embarque dans le conteneur de l'agent
et envoye directement dans son message Teams.

La regle CORS Blob a ete ajoutee en conservant les proprietes du service et les
regles existantes. Les prevols des assets et du Blob passent, les types MIME et
les SHA-256 des trois fichiers publies correspondent au build local, et les
routes `/api/chat`, `/api/blob` et `/.env` ne sont pas exposees.

Si **Check connection** fonctionne mais **Load scene** affiche `Failed to fetch`,
cela ne prouve pas une panne de l'invoke : les telechargements HTTP du GLB et
du HDR sont des etapes distinctes. La configuration initiale hachait le GUID
seul et autorisait donc une autre origine. Les deux services doivent autoriser
l'origine du MRI ci-dessus : le Blob pour le GLB, Static Web Apps pour le HDR.
Les scripts classiques et la feuille de style peuvent se charger malgre cette
erreur CORS, ce qui explique pourquoi l'interface et le ping fonctionnaient.
Ce correctif ne change ni l'HTML deja envoye, ni l'agent version 18, ni le
manifest Teams ; reessayer **Load scene** dans le widget existant suffit.
Apres correction, le navigateur telecharge le GLB original et le HDR avec HTTP
200 depuis l'origine du MRI ; le player publie atteint `Scene ready` sous une
CSP restrictive. Le parent Teams est simule dans ce controle navigateur :
la confirmation finale du bouton dans le vrai client Teams reste a faire.

Le registre ACR existant utilise `AbacRepositoryPermissions`. Un build ACR
doit donc fournir `--source-acr-auth-id '[caller]'`. Utiliser un contexte de
build minimal contenant seulement les entrees `COPY` du Dockerfile, ses
requirements et le bootstrap HTML ; ne pas envoyer les dependances Node,
environnements locaux ou secrets avec les sources.

L'image de la version 18 est epinglee sur
`480af19projdav343cacr.azurecr.io/blenderagent@sha256:843200340a3598f1de4e785591a26089857ccbe75f9ebaf7a51d225c2c5640b6`
(build ACR `cp37`). Les 17 variables de la version 17 ont ete conservees,
notamment le modele `gpt-6-astra`, le stockage, Speech et les reglages Activity.
Seule `TEAMS_WIDGET_ASSET_BASE_URL` a ete ajoutee. CPU, memoire, protocoles
Responses/Activity/`invocations_ws`, selecteur d'endpoint `@latest`,
`BotServiceRbac` et identite du bot sont inchanges.

Un smoke test Responses sur la version 18 active a repondu `OK` dans une session
isolee, supprimee ensuite. Aucun export, rendu, appel payant video ou conversation
Teams existante n'a ete utilise pour ce test. Il ne valide pas les invokes du
widget dans le client Teams : installer la mise a jour du package `1.1.1`,
demander un nouvel export GLB et utiliser **Check connection**, puis **Load scene**.

### Acces, limites et repli

Les deux seuls outils Widget sont `scene_widget_ping` et `load_scene_snapshot`.
Ils ne sont pas exposes comme outils LLM. Le serveur exige une activite Teams
authentifiee et verifie tenant, bot, conversation/thread, generation et identifiant.
Il n'utilise pas `activity.from` comme preuve d'identite individuelle.

Le chargement verifie l'existence, la taille et l'ETag du Blob avant d'emettre un
SAS de lecture d'une heure. Il reverifie `/clear` apres l'appel Storage. Aucune
operation Blender, export ou action payante n'est executee par ces invokes.
Un lien deja obtenu ou un GLB deja telecharge ne peut pas etre retire
retroactivement par `/clear` ; les nouvelles demandes et renouvellements sont refuses.

Le viewer accepte les GLB 2.0 autonomes jusqu'a 200 MiB ; il refuse les ressources
externes et les extensions Draco, Meshopt et BasisU pour eviter des chargements
implicites de decodeurs/CDN. Le temps de chargement est borne a 90 secondes.
Un echec du transport, de CORS, du WebGL ou de l'environnement laisse un message
explicite et une possibilite de reessai. Le lien GLB initial reste le repli.
Le rendu est suspendu quand le widget est masque, hors ecran ou immobile.

### Verifications locales

```powershell
.\.venv\Scripts\python.exe .\devTools\test_teams_widgets.py
.\.venv\Scripts\python.exe .\devTools\test_activity_history.py
node .\devTools\test_teams_widget.mjs
npm --prefix .\webchat run build
```

Le script Node verifie handshake, source/origine du parent, erreurs, retries,
hauteur fractionnaire, limites GLB et CORS des assets. Avec `--serve`, il ouvre
un serveur de fixture local et affiche l'URL `/widget-test` : hote Teams simule,
cube GLB synthetique et environnement HDR reel. La fixture remplace le
telechargement Blob par `/widget-test/cube.glb` ; cela ne teste pas Azure.
Le rendu WebGL reel du cube est verifie localement, pas seulement la presence
d'un canvas. Les controles orbit/pan et leur reinitialisation, les themes, la
hauteur fractionnaire responsive, le rechargement apres HTTP 403 et la
recuperation apres perte du contexte WebGL ont aussi ete verifies sur la fixture.
Les tests Python verifient la restauration du registre depuis le stockage durable
recree. La validation finale de Teams/Foundry reste celle des criteres du POC.

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
Ce document est un plan : aucune implementation Widget n'a encore ete ajoutee.

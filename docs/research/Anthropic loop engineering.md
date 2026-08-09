|     |     | Loop          | Engineering: |         |     | The  | Anthropic |     | Playbook |        |     |     |     |     |
| --- | --- | ------------- | ------------ | ------- | --- | ---- | --------- | --- | -------- | ------ | --- | --- | --- | --- |
|     |     | for Designing |              | Systems |     | That | Prompt    |     | Your     | Agents |     |     |     |     |
AFieldStudyofDesigningLoopsThatRunThemselves
Abstract—Overthepasttwoyearsastringof“XXEngineering”
|     |     |     |     |     |     |     | the system | that | feeds | it. The weight | of  | the sentence | falls | on  |
| --- | --- | --- | --- | --- | --- | --- | ---------- | ---- | ----- | -------------- | --- | ------------ | ----- | --- |
termshastrackedthepaceofmodelreleases.Thisnoteexamines replacingyourself. Thisisashiftofposition—frombeingthe
the newest of them, Loop Engineering, a term independently enginetobeingthepersonwhodesignstheengine. Whatone
surfacedinJune2026byPeterSteinberger,BorisCherny,and writes is no longer the words for the agent, but a thing that
AddyOsmani,andnamedinwritingbyOsmani. Unlikeprompt, automaticallysendswordstotheagent.
context,orharnessengineering,loopengineeringdoesnotteach
|     |     |     |     |     |     |     | B. WithinOneWeek,ThreePeopleLittheFuse |     |     |     |     |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | -------------------------------------- | --- | --- | --- | --- | --- | --- | --- |
thepractitionertodotheworkbetter;itremovesthepractitioner
fromthepositionofdoingtheworkatall. Wedefinetheterm, Thetermwasnotinventedoutofnowhere. Inasingleweek
ofJune2026,severalgroupsranintothesamethingatalmost
placeitasafourthlayerabovetheharness,anddecomposea
WhatsetitoffwasapostfromPeterSteinberger
| singleturnofaloopintofivemoves—discovery, |     |     |     |     | handoff, | ver- | thesametime. |     |     |     |     |     |     |     |
| ----------------------------------------- | --- | --- | --- | --- | -------- | ---- | ------------ | --- | --- | --- | --- | --- | --- | --- |
ification, persistence, and scheduling—and the six parts that (author of OpenClaw) which passed eight million views: one
Wegiveparticularattentiontothegenerator/eval- should no longer be prompting coding agents, but designing
realizethem.
uatorseparation: empirically,anagentaskedtogradeitsown theloopsthatpromptthem. AtnearlythesamemomentBoris
Cherny(leadonClaudeCodeatAnthropic)wassayingthesame
outputtendstopraiseit,andtuninganindependentskeptical
evaluatorisfarmoretractablethanmakingageneratorcriti- thing—thathenolongerpromptsClaude,buthasloopsrunning
calofitsownwork. Wesurveythreeloopsrunninginpractice, thatpromptClaudeandfigureoutwhattodo,andthathisjob
|     |     |     |     |     |     |     | is to write | loops. | On  | June 7 Osmani | wrote | it up | on his | blog |
| --- | --- | --- | --- | --- | --- | --- | ----------- | ------ | --- | ------------- | ----- | ----- | ------ | ---- |
fromoneengineer’smorningtriagetoStripe’senterprise-scale
pipelinemergingover1,300machine-writtenpullrequestsper underthetitleLoopEngineering, pullinginwhatSteinberger
|     |     |     |     |     |     |     | and Cherny | had | said, | and synced | it to Substack |     | the next | day. |
| --- | --- | --- | --- | --- | --- | --- | ---------- | --- | ----- | ---------- | -------------- | --- | -------- | ---- |
week,andwecatalogfourcoststhataccruesilently—verification
debt,comprehensionrot,cognitivesurrender,andtokenblowout. Oneignition,oneecho,onename,allwithinaweek.
We close with a concrete recipe for building a first loop. The Stackedtogether,thethreestatementspointatthesamemove:
whatonedesignshasshiftedfromasinglebehavioroftheagent
centralclaimisthatloopsmakegenerationnearlyfreeandleave
judgment as the scarce resource; the same loop, built by two totheentiresystemthatdrivestheagent. Aswith“vibecoding”
people,canyieldoppositeoutcomes. beforeit,thetermmaysoundcrude,butitturnsawayofworking
intosomethingthatcanbediscussed.
IndexTerms—AgenticAI,softwareengineering,autonomous
|     |     |     |     |     |     |     | C. WhytheTermArrivedNow |     |     |     |     |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | ----------------------- | --- | --- | --- | --- | --- | --- | --- |
agents,codingagents,generator–evaluator,scheduling,automa-
tion. It is worth asking why three practitioners, not comparing
|                  |     |                             |     |     |     |     | notes,reachedforthesamewordinthesameweek. |             |     |           |         |         | Theanswer    |     |
| ---------------- | --- | --------------------------- | --- | --- | --- | --- | ----------------------------------------- | ----------- | --- | --------- | ------- | ------- | ------------ | --- |
|                  |     |                             |     |     |     |     | is that the                               | surrounding |     | tools had | quietly | crossed | a threshold. |     |
| I. Introduction: |     | WhatLoopEngineeringReallyIs |     |     |     |     |                                           |             |     |           |         |         |              |     |
Codingagentshadbecomereliableenoughtofinishanon-trivial
Thepromptengineeringcoursesarestillselling,theinkon taskunattended;schedulingprimitiveshadappearedinthemajor
contextengineeringisnotyetdry,andharnessengineeringhas harnesses; and the cost of a single agent run had dropped far
| only just been | written | up. Now | loop engineering |     | arrives | as  |     |     |     |     |     |     |     |     |
| -------------- | ------- | ------- | ---------------- | --- | ------- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
enoughthatrunningonerepeatedly,onatimer,stoppedlooking
well. Acrossthepastyearthese“XXEngineering”termshave wasteful. Whenthepartsareallpresent,themovethatcombines
appearedalmostinstepwithmodelreleases,andthetemptation thembecomesobvioustoeveryoneatonce.Thenamelaggedthe
torollone’seyesisunderstandable. practicebymonths: peoplewerealreadywritingloopsbefore
Thisoneisdifferent. Itisnotaboutdoingtheworkbetter;it anyone called it loop engineering, the same way teams were
isaboutpullingthepractitioneroutofthepositionofdoingthe
alreadypairingawriteragentwitharevieweragentbeforethe
generator/evaluatorsplithadaname.
| workentirely. | Theearliertermsallassumedahumanseatedat |     |     |     |     |     |     |     |     |     |     |     |     |     |
| ------------- | --------------------------------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
thekeyboard,directingtheagentlinebyline. Loopengineering Thispattern—practicefirst,namesecond—isworthkeeping
deletesthatassumption. Thepractitionerisnolongerinsidethe inmind, becauseittellsthereaderwheretolookforthenext
loop,butoutsideit,buildingtheloop. term. It will not come from a model release. It will come
fromthemomentanewcapabilitybecomescheapenoughthata
A. AOne-LineDefinition
previouslyunthinkablecompositionbecomesroutine.
| The person                              | who | named the term | and | wrote | it up is      | Addy |                            |     |     |     |     |     |     |     |
| --------------------------------------- | --- | -------------- | --- | ----- | ------------- | ---- | -------------------------- | --- | --- | --- | --- | --- | --- | --- |
|                                         |     |                |     |       |               |      | D. OneFloorAbovetheHarness |     |     |     |     |     |     |     |
| Osmani,anengineerontheGoogleChrometeam. |     |                |     |       | Hisdefinition |      |                            |     |     |     |     |     |     |     |
is short: loop engineering is replacing oneself as the person Theshiftreducestotwosentences. Intheoldworld,onesits
who prompts the agent, and designing the system that does it andpromptstheagentlinebyline;itfinishesonething,stops,
instead. Onenolongerfeedstheagentlinebyline;onedesigns andwaits. Oneisthehumanclockinsidetheloop, andevery
©2026HuaShu.Personaluseofthismaterialispermitted.Thisisanindependentreformattingoftheauthor’sopen“OrangeBook”guideLoopEngineering:StopAskingMeWhatItIs
(v260615)intoaconference-styledocument.OriginallyreleasedJune2026;freelyavailableathuasheng.ai/orange-books.

2026WorkingNoteonAgenticSoftwareEngineeringPractice
TABLEI tosummarize,whatstaleinformationtoclear. Awindowfullof
TheFour-LayerStack
noisewasteseventhebestprompt.
| Layer | Whatitminds |     | Corequestion |     |     |     |     |     |     |     |
| ----- | ----------- | --- | ------------ | --- | --- | --- | --- | --- | --- | --- |
Harnessmindswhatanagentneedstocarryforasinglerun:
whichtools,whichactions,whentoloadcontext,howtorecover
| Prompt | Writing    | one | What should | I tell | the |                                    |     |     |                     |     |
| ------ | ---------- | --- | ----------- | ------ | --- | ---------------------------------- | --- | --- | ------------------- | --- |
|        |            |     |             |        |     | fromfailure,whatstatecountsasdone. |     |     | Itarmsonerun;itdoes |     |
| eng.   | goodprompt |     | model       |        |     |                                    |     |     |                     |     |
Context Whatgoesinthe What to retrieve, sum- notmakethatrunrepeat.
| eng.    | windownow     |     | marize,clearout     |     |     |                                      |     |     |     |              |
| ------- | ------------- | --- | ------------------- | --- | --- | ------------------------------------ | --- | --- | --- | ------------ |
|         |               |     |                     |     |     | Loopautomatesthe“waitingforyou”away. |     |     |     | Withthefirst |
| Harness | Armingasingle |     | Whichtools,whichac- |     |     |                                      |     |     |     |              |
threelayersinplace,anagentrunscleanlyoncebutthenstops.
| eng. | run |     | tions, what | counts | as  |          |                      |             |              |        |
| ---- | --- | --- | ----------- | ------ | --- | -------- | -------------------- | ----------- | ------------ | ------ |
|      |     |     |             |        |     | The loop | fits it with a timer | so it wakes | on schedule, | spawns |
done
sub-agentsforparallelwork,andfeedsitsownoutputbackas
| Loopeng. | Scheduling | on  | How to make     | it run | it- |                               |     |     |     |     |
| -------- | ---------- | --- | --------------- | ------ | --- | ----------------------------- | --- | --- | --- | --- |
|          | theharness |     | selfoverandover |        |     | thenextround’sinput.          |     |     |     |     |
|          |            |     |                 |        |     | B. WhatOneFloorUpActuallyAdds |     |     |     |     |
Loopengineering
|     | makeitrunitself,overandover |     |     |     | one |                                    |     |     |               |     |
| --- | --------------------------- | --- | --- | --- | --- | ---------------------------------- | --- | --- | ------------- | --- |
|     |                             |     |     |     |     | Threeverbsseparateharnessfromloop. |     |     | Runsonatimer: | the |
floor
|            |                    |     |     |     | up  | loopwakesonschedulewithnobutton-press. |     |     | Spawnshelpers: | a   |
| ---------- | ------------------ | --- | --- | --- | --- | -------------------------------------- | --- | --- | -------------- | --- |
| sworgepocs | Harnessengineering |     |     |     |     |                                        |     |     |                |     |
armasinglerun:tools,actions,“done” turningloopsplitsoffsub-agents—onedraftsachange,another
|     |     |     |     |     |     | doesnothingbutpickitapartinreview. |     |     | Feedsitself: | whatthe |
| --- | --- | --- | --- | --- | --- | ---------------------------------- | --- | --- | ------------ | ------- |
Contextengineering
whatgoesinthewindowrightnow loop produces becomes its own input next round; yesterday’s
findingsarewrittentoafile,andthismorningitreadsthatfile
Promptengineering
thewordsyouwriteforthemodel andcarrieson. Thismemoryacrossconversationsiswhyitisa
loopratherthanaone-offtaskrunmanytimes.
Fig.1.Thefour-layerstack.Eachlayermindssomethinglargerthan
theonebelow;loopengineeringautomatesthe“waitingforyou”that The fine-grained split matters because each layer fails dif-
theharnessleavesbehind. ferently,andthecheckthatcansay“no”mustbeinstalledina
|     |     |     |     |     |     | differentplace.        | Abadpromptiscaughtonthespot;badcontext |                            |     |     |
| --- | --- | --- | --- | --- | --- | ---------------------- | -------------------------------------- | -------------------------- | --- | --- |
|     |     |     |     |     |     | showsupinawronganswer. |                                        | Butatthelooplayerthesystem |     |     |
tickmustcomefromthehuman. Inthenewworld,onedesigns runswhileonesleeps,changescodeoneneverlookedat,and
| something | that ticks on | its own—it | runs on | a timer, | spawns |     |     |     |     |     |
| --------- | ------------- | ---------- | ------- | -------- | ------ | --- | --- | --- | --- | --- |
feedsitsownerrorsintothenextround—andthemistakemay
helperstodothework,andfeedsitsownresultsbacktoitself. goundiscoveredfordays. Thehigherthelayer,thefartheroneis
As Osmani puts it, loop engineering sits one floor above the fromthescene,andthelongermistakespileup. Thatisprecisely
whytherealdifficultyofloopengineeringisneverbuildingthe
| harness: | the harness below | arms | a single agent | run; | the loop |     |     |     |     |     |
| -------- | ----------------- | ---- | -------------- | ---- | -------- | --- | --- | --- | --- | --- |
abovemakesitrunitselfoverandover. Thechangeisoneof loop,butputtingsomethinginsideitthatcanstopit.
identity,fromthepersonwhooperatestheagenttotheperson
whoschedulesit. Valuemovesfrom“knowinghowtodirect”to C. EachLayer’sFailureHasaDifferentBlastRadius
“knowinghowtobuildloops,andhowtoputacheckinsidethe
loopthatcansayno”—thelastpartbeingthehardest,aslater Considerthesameunderlyingbug—anagentthatmisreads
sectionsexplain. what a function returns—as it manifests at each layer. At the
|     |     |     |     |     |     | prompt layer, | the misreading | produces | one wrong | answer in |
| --- | --- | --- | --- | --- | --- | ------------- | -------------- | -------- | --------- | --------- |
II. FromPrompttoContexttoLoop oneexchange;thehumanseesitimmediatelyandrewritesthe
The“XXEngineering”termsarenotreplacementsforone prompt. Atthecontextlayer,themisreadingcomesfromstale
documentationloadedintothewindow;thehumannoticesthe
| another;theystack,eachmindingsomethinglarger. |     |     |     | TableIlays |     |     |     |     |     |     |
| --------------------------------------------- | --- | --- | --- | ---------- | --- | --- | --- | --- | --- | --- |
outthefourlayers. answerisconfidentlywrongandclearsthecontext. Atthehar-
|                                           |     |     |     |     |         | ness layer, | the agent acts | on the misreading | once—perhaps | it  |
| ----------------------------------------- | --- | --- | --- | --- | ------- | ----------- | -------------- | ----------------- | ------------ | --- |
| Eachlayerup,theunitofconcerngrowsonesize: |     |     |     |     | fromone |             |                |                   |              |     |
editsafile—buttherunends,thediffisvisible,andthehuman
sentence,toonewindow,toonerun,andfinallytoaloopthat
|             |                                             |     |     |     |     | reviewsitbeforeanythingships. |     | Atthelooplayer,thesamemis- |     |     |
| ----------- | ------------------------------------------- | --- | --- | --- | --- | ----------------------------- | --- | -------------------------- | --- | --- |
| runsitself. | Fig.1showsthefourlayersasastack,withtheloop |     |     |     |     |                               |     |                            |     |     |
readingiswrittenintothestatefile,readbackthenextmorning
sittingonefloorabovetheharness.
|     |     |     |     |     |     | as established | fact, and | built upon across | many turns. | By the |
| --- | --- | --- | --- | --- | --- | -------------- | --------- | ----------------- | ----------- | ------ |
A. TheFourLayers timeanyonelooks,thewrongassumptionisload-bearing.
Promptisthebottomlayerandthebestknown.Itmindswhat Thisisthesinglemostimportantintuitioninloopengineering:
totellthemodel: wording,examples,role,tone. Itsboundary thecostofamistakescaleswiththenumberofturnsitsurvives
is one exchange. The trouble is that it assumes the human is before someone catches it, and a loop is, by construction, a
presenteverytimetohandthepromptin. machineformaximizingthenumberofturns. Everythinginthe
Contextraisesthequestionfrom“whatdoIsay”to“what latersections—theevaluator,thehumancheckpoint,thebudget
shouldgointothiswindowsothemodelcancracktheproblem.” caps—existstoshortenthedistancebetweenamistakeandits
| Itmindsthemodel’sentirefieldofview—whattoretrieve,how |     |     |     |     |     | discovery. |     |     |     |     |
| ----------------------------------------------------- | --- | --- | --- | --- | --- | ---------- | --- | --- | --- | --- |
2

2026WorkingNoteonAgenticSoftwareEngineeringPractice
TABLEII
Dis-
TheFiveMoves,MappedtotheTriageLoop
covery
|     |     |     |     |     |     |     | Move | Whatitdoes |     |     | Inthetriageloop |     |
| --- | --- | --- | --- | --- | --- | --- | ---- | ---------- | --- | --- | --------------- | --- |
/
|     |     | Sched- |     |     | Hand- |     | Discovery | Find         | this | turn’s | Skill reads  | CI is- |
| --- | --- | ------ | --- | --- | ----- | --- | --------- | ------------ | ---- | ------ | ------------ | ------ |
|     |     | uling  |     |     | off   |     |           |              |      |        |              |        |
|     |     |        |     |     |       |     |           | workonitsown |      |        | sues/commits |        |
one
|     |     |         | turn |         |     |     | Handoff      | Hand         | the | task off, | Eachfindingopens |     |
| --- | --- | ------- | ---- | ------- | --- | --- | ------------ | ------------ | --- | --------- | ---------------- | --- |
|     |     |         |      |         |     |     |              | isolated     |     |           | aworktree        |     |
|     |     |         |      |         |     |     | Verification | Swap         | in  | another   | Second sub-agent |     |
|     |     | Persis- |      | Verifi- |     |     |              | agenttosayno |     |           | reviewsvs.tests  |     |
PR+inbox+state
|     |     | tence |     | cation     |     |     | Persistence | Write           | state | outside |                  |     |
| --- | --- | ----- | --- | ---------- | --- | --- | ----------- | --------------- | ----- | ------- | ---------------- | --- |
|     |     |       |     |            |     |     |             | theconversation |       |         | file             |     |
|     |     |       |     | the“sayno” |     |     | Scheduling  | Makeitturnround |       |         | Morning automa-  |     |
|     |     |       |     |            |     |     |             | afterround      |       |         | tionrunsonitsown |     |
Fig.2.Thefivemovesofoneturn.Schedulingclosesthecycle—it
feedsanunfinishedturnintothenextday’srun.Verificationisthe
movethatcansay“no.”
leastaffordabletoskip.
Afterthefirstsub-agentdraftsthefix,a
secondsub-agentreviewsit—differentinstructions,sometimesa
TheFiveMovesofOneLoop
|     | III. |     |     |     |     |     | differentmodel. | Theagentthatwrotethecodegradesitsown |     |     |     |     |
| --- | ---- | --- | --- | --- | --- | --- | --------------- | ------------------------------------ | --- | --- | --- | --- |
Theword“loop”iseasytomisreadasidlespinning. Each homeworktoosoftly;adedicatedhole-pickercatcheswhatthe
|                            |     |     |     |                             |     |     | firsttalkeditselfintolettingthrough. |     |     |     | Thisisthe“thingthatcan |     |
| -------------------------- | --- | --- | --- | --------------------------- | --- | --- | ------------------------------------ | --- | --- | --- | ---------------------- | --- |
| turndoessomethingconcrete: |     |     |     | itfindsworkworthdoing,hands |     |     |                                      |     |     |     |                        |     |
sayno.” Aloopwithoutarealcheckisjustanagentnoddingat
| it to an agent, |     | verifies   | whether | the result | is right,  | saves state, |         |     |     |     |     |     |
| --------------- | --- | ---------- | ------- | ---------- | ---------- | ------------ | ------- | --- | --- | --- | --- | --- |
| then decides    | the | next step. | Drop    | any        | one of the | five moves—  | itself. |     |     |     |     |     |
handoff, Persistencelandstheresultsomewherethatsurvivesthecon-
| discovery, |     | verification, |     | persistence, | scheduling—and |     |     |     |     |     |     |     |
| ---------- | --- | ------------- | --- | ------------ | -------------- | --- | --- | --- | --- | --- | --- | --- |
theloopwillnotturn,orwillturninplace. Fig.2tracesthefive versation: aPRandupdatedticketviaaconnector,aninboxfor
|     |     |     |     |     |     |     | whatcannotbehandled,andastatefilerecordingprogress. |     |     |     |     | A   |
| --- | --- | --- | --- | --- | --- | --- | --------------------------------------------------- | --- | --- | --- | --- | --- |
asoneturnthatfeedsthenext.
loop’smemorycannotliveonlyinthecontextwindow;whatis
A. AConcreteExample
writtentomarkdownoraboarddoesnotforget.
|     |     |     |     |     |     |     | Schedulingiswhatmakesoneturnintoaloop. |     |     |     |     | Thetriage |
| --- | --- | --- | --- | --- | --- | --- | -------------------------------------- | --- | --- | --- | --- | --------- |
Osmanibuilthimselfamorningtriageloop.Inthemorningan
automationkicksoffonitsown. Atriageskillreadsyesterday’s runsautomaticallyeachmorning,andthestatefileletsunfinished
failingCItests,thestill-openissues,andrecentcommits,and findingscarrytothenextday, whichpicksuponitsown. As
Osmaniputsit,automationsarewhatmakealoopanactualloop
| writes its | results | into a | markdown | file | or a Linear | board. For |     |     |     |     |     |     |
| ---------- | ------- | ------ | -------- | ---- | ----------- | ---------- | --- | --- | --- | --- | --- | --- |
eachfindingworthactingonitopensanisolatedworktree;one andnotjustonerunyoudidonce. TableIIsummarizesthefive.
sub-agentdraftsthefix,asecondreviewsitagainsttheproject’s SixParts: WhataLoopIsBuiltFrom
IV.
skillsandtests.Aconnectorautomaticallyopensthepullrequest
and updates the ticket. Anything it cannot handle goes to an Ifmovesdescribewhathappensinoneturn,partsdescribe
|     |     |     |     |     |     |     | what must | be in hand | for it | to turn | at all. The | two line up: |
| --- | --- | --- | --- | --- | --- | --- | --------- | ---------- | ------ | ------- | ----------- | ------------ |
inboxtowaitforahuman,andastatefilesurvivessothenext
discoveryrunsonskills,handoffonworktrees,verificationon
| daypicksupwherethisoneleftoff. |     |     |     | Nostepneedsahand,yetit |     |     |     |     |     |     |     |     |
| ------------------------------ | --- | --- | --- | ---------------------- | --- | --- | --- | --- | --- | --- | --- | --- |
stopstowaitforahumanexactlywhereitshould. sub-agents,persistenceonmemory,schedulingonautomations.
Automationsgettheloopmovingontheirown,hangingoff
B. TheMoves
|     |     |     |     |     |     |     | ascheduleortrigger. | Withoutscheduling,whatoneholdsisa |     |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | ------------------- | --------------------------------- | --- | --- | --- | --- |
Discovery figures out what this turn should do. In the ex- singlerun,notaloop. Theautomationshouldtriggeranamed
ample,thetriageskillreadsCIfailures,openissues,andrecent skill,notawallofinstructionsinacronjob. Schedulingcomes
commits. Thekeyislettingtheagentfinditsownworkrather inmorethanoneform—local(machinemuststayon)andcloud
(turnsevenwiththemachineoff).
| than being | handed | a list. | Crucially, | the | automation | triggers a |     |     |     |     |     |     |
| ---------- | ------ | ------- | ---------- | --- | ---------- | ---------- | --- | --- | --- | --- | --- | --- |
skill—knowledge made permanent—rather than a wall of in- Worktrees are a built-in git mechanism for multiple inde-
structionspastedintoacronjobnobodywillupdate. Discovery pendent working directories in one repo. Their value scales
setstheceilingonthewholeloop’squality: surfaceworkofno withparallelism: twoagentswritingthesamefileatonceisthe
valueandtheotherfourmovesaredonebeautifullyinserviceof sameheadacheastwoengineerscommittingtothesamelines.
nothing. Worktreesturnparallelismfrom“runsbutmessy”into“runsand
| Handoff | movesthetaskfromtheschedulingsystemintothe |     |     |     |     |     | clean.” |     |     |     |     |     |
| ------- | ------------------------------------------ | --- | --- | --- | --- | --- | ------- | --- | --- | --- | --- | --- |
handsoftheagentthatdoesthework. Eachfindingworthdoing Skills make project knowledge permanent in a single file
gets its own isolated git worktree, so multiple agents change (SKILL.md),sotheagentneednotre-derivecontexteveryturn.
codeinseparatedirectorieswithoutsteppingoneachother. The Osmaninamesthecosttheypayoffasintentdebt: thepriceof
cleanereachtaskiscut,theeasierverificationandmergingare explaining“whatthisprojectis,whattherulesare,wherethe
later. trapsare”overandover. Askillcanbereusedandmaintained;a
Verificationistheeasiestmovetocutcornersonandtheone wallofpromptcannot.
3

2026WorkingNoteonAgenticSoftwareEngineeringPractice
|     |     | TABLEIII |     |     |     |     |     |     | draft |     |     |
| --- | --- | -------- | --- | --- | --- | --- | --- | --- | ----- | --- | --- |
SixPartsMappedtoFiveMoves
maker–checker
| Part        | Whatitis         |     | Mapstomove |     |     |     |     |               |     |                 |     |
| ----------- | ---------------- | --- | ---------- | --- | --- | --- | --- | ------------- | --- | --------------- | --- |
|             |                  |     |            |     |     |     |     | Generator     |     | Evaluator       |     |
|             | Runsoffaschedule |     |            |     |     |     |     |               |     | differentmodel; |     |
| Automations |                  |     | Scheduling |     |     |     |     | writesthecode |     |                 |     |
assumesbroken
/trigger
| Worktrees | Isolateddirsforpar- |     | Handoff |     |     |     |     |     |     |     |     |
| --------- | ------------------- | --- | ------- | --- | --- | --- | --- | --- | --- | --- | --- |
actsviaMCP:click,screenshot,runtests
allelagents
reject+reasons
| Skills | Permanent | knowl- | Discovery |     |     |     |     |     |     |     |     |
| ------ | --------- | ------ | --------- | --- | --- | --- | --- | --- | --- | --- | --- |
edge;paysoffintent Fig.3.Generatorandevaluatorasseparateagents.Theevaluator
carriesnoneofthegenerator’sself-persuasion,defaultstodoubt,and
debt
judgesbehaviorbyactingratherthanjustreading.
| Connectors | MCPhookuptoex-     |     | Persistence  |     | / Dis- |     |     |     |     |     |     |
| ---------- | ------------------ | --- | ------------ | --- | ------ | --- | --- | --- | --- | --- | --- |
|            | ternalsystems      |     | covery       |     |        |     |     |     |     |     |     |
| Sub-       | Generatorseparated |     | Verification |     |        |     |     |     |     |     |     |
inwhichthecodewaswrittenisalreadystuffedwiththereasons
| agents | fromjudge |     |     |     |     |     |     |     |     |     |     |
| ------ | --------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
itwaswrittenthatway,sowhentheagentlooksatitsownoutput
| Memory | Persistent | state on | Persistence |     |     |     |                                                       |                                |     |     |            |
| ------ | ---------- | -------- | ----------- | --- | --- | --- | ----------------------------------------------------- | ------------------------------ | --- | --- | ---------- |
|        | disk       |          |             |     |     |     | itdoesnotseetheresult—itseesthechainofself-persuasion |                                |     |     |            |
|        |            |          |             |     |     |     | thatledthere.                                         | Insidealooptheflawisamplified: |     |     | ifevery“is |
thisgoodenough”isdecidedbytheagentthatjustwroteit,each
Connectors(builtonMCP,theModelContextProtocol)hook rounditnodsatitself,andthelongeritrunsthefurtheritdrifts
fromrealquality.
thelooptotheoutsideworld—theissuetracker,thedatabase,a
| stagingAPI,Slack.                                      | Aloopthatcanonlyseethefilesystemisa |     |     |     |     |     |                                       |               |                    |       |             |
| ------------------------------------------------------ | ----------------------------------- | --- | --- | --- | --- | --- | ------------------------------------- | ------------- | ------------------ | ----- | ----------- |
|                                                        |                                     |     |     |     |     |     | B. TuneaSkeptic,Don’tFixaModestAuthor |               |                    |       |             |
| tinyloop. Connectorsdecidetheloop’sradiusofvision,anda |                                     |     |     |     |     |     |                                       |               |                    |       |             |
|                                                        |                                     |     |     |     |     |     | Making                                | the generator | more self-critical | works | poorly. Ra- |
connectorwrittenforonetoolcanoftenbedroppedontoanother
jasekaranfoundthattuningastandaloneevaluatortobeskeptical
andusedasis.
isfarmoretractablethanmakingageneratorcriticalofitsown
Sub-agentssplittheonethatwritesfromtheonethatjudges.
|          |                      |     |          |     |         |       | work. | Thedifferenceisstructural,notamatterofwording: |     |     | one |
| -------- | -------------------- | --- | -------- | --- | ------- | ----- | ----- | ---------------------------------------------- | --- | --- | --- |
| When one | agent is both player | and | referee, | the | referee | plays |       |                                                |     |     |     |
cannotaskanauthortostepoutsideitsownperspective,butone
| favorites. Counterintuitively,tuninganindependentjudgetobe |                     |              |     |     |         |        |            |                  |               |           |                   |
| ---------------------------------------------------------- | ------------------- | ------------ | --- | --- | ------- | ------ | ---------- | ---------------- | ------------- | --------- | ----------------- |
|                                                            |                     |              |     |     |         |        | can swap   | in another agent | with entirely | different | instructions      |
| picky is far                                               | easier than getting | thegenerator |     | to  | be hard | on its |            |                  |               |           |                   |
|                                                            |                     |              |     |     |         |        | that looks | at the code      | from scratch, | carrying  | none of the self- |
ownwork—whichiswhyaloopkeepsanextraagentratherthan
|     |     |     |     |     |     |     | persuasion. | Theideaisborrowedfromgenerativeadversarial |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | ----------- | ------------------------------------------ | --- | --- | --- |
lettingoneagentaudititself.
networks(GANs)—onenetworkbuilds,onepicksfaults—ported
| Memory                              | is persistent | state | living | outside        | a   | single |                                                  |     |     |     |       |
| ----------------------------------- | ------------- | ----- | ------ | -------------- | --- | ------ | ------------------------------------------------ | --- | --- | --- | ----- |
|                                     |               |       |        |                |     |        | toageneratorthatwritesandanevaluatorthatreviews. |     |     |     | Fig.3 |
| conversation—amarkdownfileoraboard. |               |       |        | Themomentacon- |     |        |                                                  |     |     |     |       |
showstheloopthisforms.
textwindowiscleared,theagentremembersnothing;foraloop
topickuptodaywhereitleftoffyesterday,memorymustland
|                                         |     |     |     |             |     |     | C. TheEvaluatorShouldAct,NotJustRead |     |                         |     |     |
| --------------------------------------- | --- | --- | --- | ----------- | --- | --- | ------------------------------------ | --- | ----------------------- | --- | --- |
| ondisk. Theagentforgets,therepodoesnot. |     |     |     | Memoryisnot |     |     |                                      |     |                         |     |     |
|                                         |     |     |     |             |     |     | Swappingagentsisnotenough.           |     | Iftheevaluatoronlyreads |     |     |
context: contextiswhattheagentseesthisroundandisflushed
|     |     |     |     |     |     |     | codeitjudges“doesthislookright,”not“doesitrunright.” |     |     |     | On  |
| --- | --- | --- | --- | --- | --- | --- | ---------------------------------------------------- | --- | --- | --- | --- |
onrefresh;memorypersistsacrossroundsanddays. TableIII frontendtasksRajasekaranhookedtheevaluatortoPlaywright
mapsthesixpartstothefivemoves.
MCPsoitcouldopenthepage,clickbuttons,takescreenshots,
| Withallsixinplacealoophasaskeleton: |     |     |     | automationmakes |     |     |     |     |     |     |     |
| ----------------------------------- | --- | --- | --- | --------------- | --- | --- | --- | --- | --- | --- | --- |
andinspecttheDOMlikeaQAengineer.Thatshiftsthebasisfor
| it move, worktrees | keep it | from fighting |     | itself, | skills | keep it |     |     |     |     |     |
| ------------------ | ------- | ------------- | --- | ------- | ------ | ------- | --- | --- | --- | --- | --- |
judgmentfrom“thisJSXlooksfine”to“Iclickedthebutton,the
fromredoingwork,connectorsletitseeoutside,sub-agentslet
pagenavigated,hereisthescreenshot.”Swappingtheunderlying
itcorrectitself,andmemoryletsitremember. Butbuildingit modeltoohelps: thesamemodelwithnewinstructionsoften
| isonlythestart: | thesamesetofparts,builtbytwopeople,can |     |     |     |     |     |                     |                                     |     |     |     |
| --------------- | -------------------------------------- | --- | --- | --- | --- | --- | ------------------- | ----------------------------------- | --- | --- | --- |
|                 |                                        |     |     |     |     |     | keepsitsblindspots. | Acommoncommunitycalibrationtellsthe |     |     |     |
comeoutcompletelyopposite.
evaluatortoassumethecodeisbrokenuntilprovenotherwise—
GeneratorandEvaluator thedefaultstanceshouldbedoubt,nottrust.
V.
|             |                |        |         |     |       |         | D. InaProduct: | /goalontheStopCondition |     |     |     |
| ----------- | -------------- | ------ | ------- | --- | ----- | ------- | -------------- | ----------------------- | --- | --- | --- |
| The hardest | part of a loop | is not | getting | the | agent | to run, |                |                         |     |     |     |
butputtingsomethinginsidethatcansay“no”—andtheagent ClaudeCodeturnsthisstructureintoaprimitivewith/goal:
writingthecodeistheoneleastlikelytosayit. giveanagentaconditionandletitrununtiltheconditionismet.
Arepresentativeevaluatorsetupandstopconditionlooklikethe
| A. ItAlwaysPraisesItself |     |     |     |     |     |     | following. |     |     |     |     |
| ------------------------ | --- | --- | --- | --- | --- | --- | ---------- | --- | --- | --- | --- |
Askanagenttogradewhatitjustproducedandittendsto
praise it confidently, even when a human can plainly see the # Evaluator agent (.claude/agents/reviewer.md)
|     |     |     |     |     |     |     | ROLE: | Adversarial code reviewer. |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | ----- | -------------------------- | --- | --- | --- |
qualityismediocre,asAnthropicengineerPrithviRajasekaran ASSUME: this code is BROKEN until proven otherwise.
observedwhilebuildinglong-runningapplications. Thisisnota DO NOT praise. Find what fails.
| smartsproblem;itisgradingone’sownhomework. |     |     |     |     | Thecontext |     |        |           |     |     |     |
| ------------------------------------------ | --- | --- | --- | --- | ---------- | --- | ------ | --------- | --- | --- | --- |
|                                            |     |     |     |     |            |     | CHECK, | in order: |     |     |     |
4

2026WorkingNoteonAgenticSoftwareEngineeringPractice
skip
| 1. Does   | it run? (execute, | don’t read)  |     |     | Discovery |      | Blindloop   |     |     |
| --------- | ----------------- | ------------ | --- | --- | --------- | ---- | ----------- | --- | --- |
| 2. Tests: | run them, paste   | real output. |     |     |           |      |             |     |     |
| 3. Edge   | cases the author  | skipped.     |     |     |           | skip |             |     |     |
| 4. Does   | behavior match    | the ticket?  |     |     | Handoff   |      | Tangledloop |     |     |
skip
USE Playwright MCP: open the page, click, Verification Noddingloop
| screenshot, | inspect the        | DOM. Judge behavior, |     |     |             |      |              |     |     |
| ----------- | ------------------ | -------------------- | --- | --- | ----------- | ---- | ------------ | --- | --- |
| not intent. |                    |                      |     |     |             | skip |              |     |     |
|             |                    |                      |     |     | Persistence |      | Amnesiacloop |     |     |
| VERDICT:    | PASS only if every | check holds.         |     |     |             |      |              |     |     |
skip
| Otherwise | REJECT + list | each reason. |     |     | Scheduling |     | Manualloop |     |     |
| --------- | ------------- | ------------ | --- | --- | ---------- | --- | ---------- | --- | --- |
Fig.4.Eachanti-patternisonemoveskipped.Thefivefailuresmap
one-to-oneontothefivemovesofasingleturn.
| # Stop condition, | judged             | by a fresh small model |     |     |     |     |     |     |     |
| ----------------- | ------------------ | ---------------------- | --- | --- | --- | --- | --- | --- | --- |
| /goal all         | tests in test/auth | pass and the lint      |     |     |     |     |     |     |     |
| step              | is clean           |                        |     |     |     |     |     |     |     |
C. TheManualLoop(schedulingskipped)
Crucially,aftereachturnasmallfastmodelcheckswhetherthe
Aloopwithfourgoodmovesbutnoautomationisnotaloop;
| condition | holds; if not,                    | another turn runs | instead of returning |                |           |              |          |         |        |
| --------- | --------------------------------- | ----------------- | -------------------- | -------------- | --------- | ------------ | -------- | ------- | ------ |
|           |                                   |                   |                      | it is a script | the human | runs by hand | and then | forgets | to run |
| control.  | Completionisdecidedbyafreshmodel, |                   | nottheone            |                |           |              |          |         |        |
again. Itworksimpressivelythedayitisbuiltandsilentlystops
| doingthework. | Thisisthemaker–checkerprinciple—decades |     |     |                         |     |                            |     |     |     |
| ------------- | --------------------------------------- | --- | --- | ----------------------- | --- | -------------------------- | --- | --- | --- |
|               |                                         |     |     | thedayattentionwanders. |     | Thesymptomisaloopwhoselast |     |     |     |
oldinbanking,wherethepersonenteringalargetransferandthe
personreviewingitmustdiffer—appliedtothestopcondition. runwasthedayitwasdemoed. Thefixisarealtrigger—atimer
oranevent—thatdoesnotdependonthehumanremembering.
(Codexreachesthesamecapabilitythroughautomationsplus
agentconfiguration;oneshouldnotconfuse/goalwith/loop,
D. TheBlindLoop(discoveryskipped)
whichmerelyrerunsonaninterval.)
Thehumanstillhandstheloopitsworkeachmorning—“fix
Aloop’sfloorisitsevaluator. Thegenerator’sleveldecides thesethreebugs”—sotheloophasautomatedthedoingbutnot
| whataloopcanproduce; |     | theevaluator’sleveldecideswhatit |     |             |                                              |     |     |     |     |
| -------------------- | --- | -------------------------------- | --- | ----------- | -------------------------------------------- | --- | --- | --- | --- |
|                      |     |                                  |     | thefinding. | Thissaveslessthanitappearsto,becausechoosing |     |     |     |     |
willnotproduce.Separategenerationfromjudgmentstructurally,
|     |     |     |     | what to | work on is often | the expensive | part. | The symptom | is  |
| --- | --- | --- | --- | ------- | ---------------- | ------------- | ----- | ----------- | --- |
tunetheevaluatorintoaskeptic,makeitverifybyacting,and ahumanwhoisstillspendingtheirmorningdecidingwhatthe
handthefinalsaytoafreshmodel—thosefourstepsarewhatit
|                                     |     |     |     | loopshoulddo.           | Thefixistoteachdiscoveryintoaskillsothe |     |     |     |     |
| ----------------------------------- | --- | --- | --- | ----------------------- | --------------------------------------- | --- | --- | --- | --- |
| takestogrowaloop’sabilitytosay“no.” |     |     |     | loopsurfacesitsownwork. |                                         |     |     |     |     |
TheTangledLoop(handoffskipped)
E.
VI. FiveWaysaLoopGoesWrong
|     |     |     |     | The | loop runs several | agents in | parallel | but lets | them all |
| --- | --- | --- | --- | --- | ----------------- | --------- | -------- | -------- | -------- |
Beforeturningtoloopsthatwork,itisworthcataloguingthe change the same working directory, so their edits collide and
| ways they | fail, because | the failures are more | instructive than |                                  |     |     |                 |     |     |
| --------- | ------------- | --------------------- | ---------------- | -------------------------------- | --- | --- | --------------- | --- | --- |
|           |               |                       |                  | themergeisamessnoonecanuntangle. |     |     | Thesymptomshows |     |     |
thesuccessesandfarmorecommon. Eachanti-patternbelow uponlyunderparallelism: asingle-agentlooplooksfine,and
| corresponds | to one of | the five moves being | skipped or done |             |             |               |             |     |          |
| ----------- | --------- | -------------------- | --------------- | ----------- | ----------- | ------------- | ----------- | --- | -------- |
|             |           |                      |                 | the problem | appears the | first morning | five agents | run | at once. |
badly.
|     |     |     |     | Thefixisoneisolatedworktreepertask. |     |     | Fig.4mapsthefive |     |     |
| --- | --- | --- | --- | ----------------------------------- | --- | --- | ---------------- | --- | --- |
anti-patternstothemovestheyviolate.
A. TheNoddingLoop(verificationskipped)
|                       |     |                            |     | Thesefivearenotindependent. |                      | Aloopmissingverification |        |          |       |
| --------------------- | --- | -------------------------- | --- | --------------------------- | -------------------- | ------------------------ | ------ | -------- | ----- |
|                       |     |                            |     | tends also                  | to miss persistence, | because                  | a team | careless | about |
| Themostcommonfailure. |     | Theloopruns,theagentwrites |     |                             |                      |                          |        |          |       |
code,andthesameagentdeclaresitgood. Withnoindependent onecheckisusuallycarelessabouttheothers. Inpracticethey
cluster: thedisciplinedloopinstallsallfivemoves,andthehasty
check,everyturnproducesself-approvedoutput,andtheloop
loopinstallsonlydiscoveryandhandoff—thetwothatproduce
| accumulatesplausible-lookingmistakesatmachinespeed. |     |     | The |     |     |     |     |     |     |
| --------------------------------------------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
symptomisaloopthathasneveroncesaid“no”toitselfacross visibleoutput—andskipsthethreethatproducesafety. Thenext
sectionshowsthreeloopsthatinstalledallfive.
hundredsofturns—astatisticalimpossibilityforanyrealwork-
| load,andthereforeproofthatnorealcheckexists. |     |     | Thefixisthe |      |                            |     |               |     |     |
| -------------------------------------------- | --- | --- | ----------- | ---- | -------------------------- | --- | ------------- | --- | --- |
|                                              |     |     |             | VII. | LoopsThatRunWhileYouSleep: |     | ThreeRealOnes |     |     |
generator/evaluatorsplitoftheprevioussection.
Threepubliccasesdifferwildlyinscalebutshareoneskele-
B. TheAmnesiacLoop(persistenceskipped) ton: atriggerpressesstart,asetofconstraintskeepsitonthe
|     |     |     |     | rails,andahumancheckpointsitsattheend. |     |     |     | “Runningwhile |     |
| --- | --- | --- | --- | -------------------------------------- | --- | --- | --- | ------------- | --- |
The loop discovers good work, does it, and then forgets it yousleep”wasneverabouthowstrongthemodelis—itisabout
happened,becausetheresultlivedonlyinacontextwindowthat
howsolidthatskeletonis.
| wasflushed. | Thenextturnrediscoversthesamework,orworse, |     |     |     |     |     |     |     |     |
| ----------- | ------------------------------------------ | --- | --- | --- | --- | --- | --- | --- | --- |
A. OneEngineer’sMorning
| redoesitandconflictswiththefirstattempt. |     |     | Thesymptomisa |     |     |     |     |     |     |
| ---------------------------------------- | --- | --- | ------------- | --- | --- | --- | --- | --- | --- |
loopthatmakesnocumulativeprogress: eachmorningitstarts Osmani’s triage loop, broken down in Section III, runs au-
fromthesameplace. Thefixisastatefileondisk—theagent tomatically every morning. The one detail worth re-flagging:
forgets,therepodoesnot. theautomationinvokesaskill,notagiantblockofinstructions
5

2026WorkingNoteonAgenticSoftwareEngineeringPractice
TABLEIV
Humantrigger—@botinSlack/emojireaction
SchedulingOptionsCompared
Deterministicorchestrator—scanlinks,pullJira, Cloud Desktop /loop
Sourcegraph+MCPassemblecontext(nottheLLM)
|     |     |     |     |     | Whereitruns |     | cloud | machine | machine |     |
| --- | --- | --- | --- | --- | ----------- | --- | ----- | ------- | ------- | --- |
LLMagent—writescodewithmaterialsinhand
|     |     |     |     |     | Machineon?   |     | no  | yes | yes |     |
| --- | --- | --- | --- | --- | ------------ | --- | --- | --- | --- | --- |
|     |     |     |     |     | Sessionopen? |     | no  | no  | yes |     |
Hard-codedgate—linterruns;agentcannotskip
|     |     |     |     |     | Min.interval   |     | 1h  | 1min | 1min |     |
| --- | --- | --- | --- | --- | -------------- | --- | --- | ---- | ---- | --- |
|     |     |     |     |     | Seelocalfiles? |     | no  | yes  | yes  |     |
LLMagent—fixesthelint
Hard-codedstep—gitcommit
|     |     |     |     | themachineon. |     | Wantittorununtetheredfromlocalstate? |     |     |     | Go  |
| --- | --- | --- | --- | ------------- | --- | ------------------------------------ | --- | --- | --- | --- |
Humanreview—1,300PRs/week tothecloud,atthecostofaone-hourminimumintervalanda
|     |     |     |     | cleancloneeachtime. |     |     | Nosingleschedulerdoesitall. |     |     |     |
| --- | --- | --- | --- | ------------------- | --- | --- | --------------------------- | --- | --- | --- |
Fig.5.Stripe’sMinionspipeline.Deterministicgates(blue)andLLM
|     |     |     |     | A   | caution | on widely | circulated | numbers: | claims | such as |
| --- | --- | --- | --- | --- | ------- | --------- | ---------- | -------- | ------ | ------- |
steps(green)interlock;anythingrule-boundiskeptoutofthe
“around90%ofClaudeCodeiswrittenbyitself”orlargemigra-
probabilisticmodel.Reliabilitycomesfromtheconstraints,notmodel
| size. |     |     |     | tionspeedupsaremostlysecondhandsummariesandshouldbe |     |     |     |     |     |     |
| ----- | --- | --- | --- | --------------------------------------------------- | --- | --- | --- | --- | --- | --- |
treatedasroughreference.Thethreecasesheretracetofirsthand
|                                             |     |     |            | sources, | which | holds | up better | than one | impressive-sounding |     |
| ------------------------------------------- | --- | --- | ---------- | -------- | ----- | ----- | --------- | -------- | ------------------- | --- |
| gluedintoaschedulethatnobodywilleverupdate. |     |     | Thisiswhat | figure.  |       |       |           |          |                     |     |
alooponepersoncanrunlookslike—oneperson,onemachine,
D. ChoosingaSchedulerinPractice
thegruntworkdoneeachmorning.
Thechoicebetweenlocalandcloudschedulingisnotamatter
| B. Stripe’sMinions: | 1,300PRsaWeek |     |     |                                               |     |     |     |     |     |             |
| ------------------- | ------------- | --- | --- | --------------------------------------------- | --- | --- | --- | --- | --- | ----------- |
|                     |               |     |     | oftaste;itfollowsmechanicallyfromonequestion: |     |     |     |     |     | istheloop’s |
For enterprise scale, the case to study is Stripe’s Minions: workgluedtothelocalmachine,orcanitleave? Twoconcrete
more than 1,300 pull requests merged a week, not one line scenarios make the rule clear. Suppose a loop must check a
writtenbyhand,asdescribedbyStripeengineerSteveKaliskion localdevelopmentservereveryminute—thatworkcanonlyrun
theHowIAI podcast. Thetriggerislight—@thebotinSlack, locally,becausethecloudcannotseeaprocessonone’slaptop
oraddanemojireaction. Whatmakesitreliableisthestretch andthecloudintervalcannotdropbelowanhour. Nowflipit:
before the model wakes up: a deterministic orchestrator first supposealoopshouldscantherepository’sopenissuesatthree
assemblescontext,scanninglinks,pullingJira,findingdocs,and inthemorningandopenpullrequestswherewarranted—that
usingSourcegraphplusMCPtolocaterelevantcode. Letting workshouldneverbetiedtoalaptopatall,becauselaptopsget
theLLMfinditsowncontextistheleastcontrollablepart,so theirlidsclosed,losepower,andgetcarriedoutthedoor. For
thatwork—whoserulescanbehard-coded—istakenoutofthe thesecond,acloudscheduleoraCIscheduletriggeristheright
model’s hands. Anything deterministic logic can solve never answer,runningonamachinethatstaysawakewhilethehuman
| goestoaprobabilisticmodel;whereonedrawsthatlinedecides |     |     |     | sleeps. |     |     |     |     |     |     |
| ------------------------------------------------------ | --- | --- | --- | ------- | --- | --- | --- | --- | --- | --- |
whethertheloopisreliable. Thedistortiontoavoidistreatinglocalrerunasthewholeof
The most counterintuitive point: Minions is not built on a “runningwhileyousleep.” Alocalrerunmeans“runafewextra
strongermodel. Itisaforkoftheopen-sourcetoolGoose,and rounds while I am here”; cloud scheduling means “run even
its core claim is that reliability comes from the quality of the whenIamnot.” Thesearedifferentcapabilities,andconflating
constraints, not the size of the model. Its architecture inter- them is how people end up disappointed when they close the
leavesdeterministicgatesandcreativeLLMsteps,assketched lid and the loop they thought was autonomous quietly stops.
in Fig. 5—the agent writes code, a hard-coded pipeline runs Thehonestframingisthatlocalschedulingbuysfrequencyand
thelinterandtheagentcannotskipit, theagentfixesthelint, accesstolocalfilesatthecostofrequiringthemachinetostay
andahard-codedsteprunsthecommit. ThesandboxisDevbox on,whilecloudschedulingbuystrueautonomyatthecostofa
onEC2, runona“cattlenotpets”basis: eachenvironmentis coarserintervalandafreshcloneeachrun. Nosinglescheduler
swappedoutatwill,soathousand-plusagentsrunatoncewith- doesitall,andamatureloopoftenusesboth—localforthetight
outsteppingoneachother. Notably,those1,300PRsarestill innerchecks,cloudfortheovernightsweep.
| reviewed | by humans—the | human did | not leave, but changed |     |     |     |     |     |     |     |
| -------- | ------------- | --------- | ---------------------- | --- | --- | --- | --- | --- | --- | --- |
E. TheSameCapability,TwoToolchains
desks,fromwritingtoreviewing.
ThecommandsinthisnoteareClaudeCode’s,butthecapa-
C. What“WhileYouSleep”ActuallyReliesOn bilitiesarenotspecifictoit. Codexoffersthesamefiveorgans
underdifferentnames,andaconnectorwrittenforonesidecan
Local/loopanddesktopscheduledtasksneedthemachine
on;turnitoffandtheloopstops. Torunwiththemachineoff, oftenbemovedtotheotherandusedasis. TableVlinesthem
therightanswerisCloudRoutinesorGitHubActionsschedule upsothereaderdoesnotpinacommandnameonthewrong
triggers. TableIVcontraststheoptions. Wantitfrequentand door. Thelessonisthatloopengineeringisasetofcapabilities,
abletoseelocalfiles? Uselocal/loop,atthecostofkeeping notaproduct: scheduling,run-until-condition,parallelisolation,
6

2026WorkingNoteonAgenticSoftwareEngineeringPractice
TABLEV
|     |     |     |     |     |     |     |     | Verification |     | Comprehension |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | --- | ------------ | --- | ------------- | --- | --- | --- |
TheSameCapabilityAcrossTwoToolchains
|             |     |             |     |                |     |     |     | debt |           |     | rot |     |     |
| ----------- | --- | ----------- | --- | -------------- | --- | --- | --- | ---- | --------- | --- | --- | --- | --- |
| Capability  |     | ClaudeCode  |     | Codex          |     |     |     |      |           |     |     |     |     |
| Scheduling  |     | /loopworker |     | Automationstab |     |     |     |      | eachfeeds |     |     |     |     |
| Rununtilmet |     | /goal       |     | automation     |     |     |     |      | thenext   |     |     |     |     |
rerun+judge
| Parallelisolation |     | --worktree |     | background |     |     |     |       |     |           |     |     |     |
| ----------------- | --- | ---------- | --- | ---------- | --- | --- | --- | ----- | --- | --------- | --- | --- | --- |
|                   |     |            |     |            |     |     |     | Token |     | Cognitive |     |     |     |
worktree
|            |     |                               |     |     |     |     |     | blowout |     | surrender |     |     |     |
| ---------- | --- | ----------------------------- | --- | --- | --- | --- | --- | ------- | --- | --------- | --- | --- | --- |
| Sub-agents |     | .claude/agents/.codex/agents/ |     |     |     |     |     |         |     |           |     |     |     |
MCP+plugins Fig.6.Thefourcostsreinforceoneanother.Unverifiedoutputerodes
| Externalconn. |     |     |     | MCPconnector |     |     |     |     |     |     |     |     |     |
| ------------- | --- | --- | --- | ------------ | --- | --- | --- | --- | --- | --- | --- | --- | --- |
understanding,whichinvitessurrender,whichletsthelooprunlonger
| Explicitskill |     | SKILL.md |     | $skill-name |     |     |     |     |     |     |     |     |     |
| ------------- | --- | -------- | --- | ----------- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
andspendmore,whichproducesmoreunverifiedoutput.
Machine-offrun
|             |          | CloudRoutines |     | cloud(planned)             |     |                                    |     |     |     |     |     |     |     |
| ----------- | -------- | ------------- | --- | -------------------------- | --- | ---------------------------------- | --- | --- | --- | --- | --- | --- | --- |
|             |          |               |     |                            |     | A. AWorkedExampleofCompoundingDebt |     |     |     |     |     |     |     |
| sub-agents, | external | connection,   | and | explicit skill invocation. |     |                                    |     |     |     |     |     |     |     |
Whichevertoolchainateamuses,thequestiontoaskiswhether Consideraloopthatopenstwentypullrequestsovernight,all
|     |     |     |     |     |     | withgreentests. |     | Onthesurfacethisisatriumph. |     |     |     | Butsuppose |     |
| --- | --- | --- | --- | --- | --- | --------------- | --- | --------------------------- | --- | --- | --- | ---------- | --- |
allsixarepresent,notwhichbrandofcommandprovidesthem.
threeofthetwentycontainasubtleerrorthetestsdonotcover.
| TheCosts: | FourTabsThatDon’tClearThemselves |     |     |     |     |                                                        |                                       |     |     |     |     |     |     |
| --------- | -------------------------------- | --- | --- | --- | --- | ------------------------------------------------------ | ------------------------------------- | --- | --- | --- | --- | --- | --- |
| VIII.     |                                  |     |     |     |     | Withnoindependentevaluator,thosethreemerge—thatisveri- |                                       |     |     |     |     |     |     |
|           |                                  |     |     |     |     | ficationdebt.                                          | BecausethehumanmergedtwentyPRswithout |     |     |     |     |     |     |
Aloopthatrunsitselfis,atthesametime,aloopthatmakes
readingthem,theirmentalmodelofthecodebasenowlagsby
| mistakesbyitself. | Themorecheerfullyitruns,themorequietly |     |     |     |     |                                       |     |     |     |     |                |     |     |
| ----------------- | -------------------------------------- | --- | --- | --- | --- | ------------------------------------- | --- | --- | --- | --- | -------------- | --- | --- |
|                   |                                        |     |     |     |     | twentychanges—thatiscomprehensionrot. |     |     |     |     | Becausetheloop |     |     |
iterrs. Fourcostsaccrue,noneofwhichsoundsanalarmwhile
|     |     |     |     |     |     | ran so smoothly, |     | the human | stops | reading | the next | morning’s |     |
| --- | --- | --- | --- | --- | --- | ---------------- | --- | --------- | ----- | ------- | -------- | --------- | --- |
theloopisrunning.
batchentirely—thatiscognitivesurrender.Andbecausetheloop
| Verificationdebt. |     | EveryPRopenedandmergedsavestime, |     |     |     |     |     |     |     |     |     |     |     |
| ----------------- | --- | -------------------------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
spawnedhelpersandretriedfreelyallnight,thebillistriplewhat
butthesavedtimeturnsintounverifiedoutputwaitingtobepaid
|                                            |     |     |     |          |     | wasestimated—thatistokenblowout. |               |     |           | Thethreeburiederrors |       |              |     |
| ------------------------------------------ | --- | --- | --- | -------- | --- | -------------------------------- | ------------- | --- | --------- | -------------------- | ----- | ------------ | --- |
| back. Theproblemhideswheretestsdonotcover, |     |     |     | inthegap |     |                                  |               |     |           |                      |       |              |     |
|                                            |     |     |     |          |     | now sit                          | in a codebase |     | the human | no longer            | fully | understands, |     |
between“runs”and“right,”accumulatinguntilsomeshipping
guardedbyahumanwhohasstoppedlooking,discoveredeven-
| morningwhenitblowsupatonce. |     |     | Theguardisanindependent |     |     |     |     |     |     |     |     |     |     |
| --------------------------- | --- | --- | ----------------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
evaluator—adifferentagentfromtheonedoingthework. tuallyonlywhenoneofthemsurfacesasaproductionincident.
Thepointoftheworkedexampleisthatthefourcostsarenot
| Comprehensionrot. |     | Thefastertheloopshipscodeonedid |     |     |     |     |     |     |     |     |     |     |     |
| ----------------- | --- | ------------------------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
alistofindependentrisks;theyareasinglefailurethatwears
notwrite,thebiggerthegapbetweenwhatexistsandwhatone
|                      |     |                                    |     |     |     | four faces. | They | reinforce | one | another: | the more | output | the |
| -------------------- | --- | ---------------------------------- | --- | --- | --- | ----------- | ---- | --------- | --- | -------- | -------- | ------ | --- |
| actuallyunderstands. |     | Readingcodeismoreboringthanwriting |     |     |     |             |      |           |     |          |          |        |     |
loopproducesunverified,thelessthehumanunderstands;the
it,andtheloophastakenthewriting;thecodebasegrowswhile
|                              |      |         |                           |                |       | less they  | understand,                    |     | the more | they surrender; |                 | the more | they   |
| ---------------------------- | ---- | ------- | ------------------------- | -------------- | ----- | ---------- | ------------------------------ | --- | -------- | --------------- | --------------- | -------- | ------ |
| the map in one’s             | head | stalls. | It sounds                 | no alarm until | a bug |            |                                |     |          |                 |                 |          |        |
|                              |      |         |                           |                |       | surrender, | the longer                     |     | the loop | runs unwatched  |                 | and the  | bigger |
| burrowsintoacornerneverread. |      |         | Theguardistoreadtheloop’s |                |       |            |                                |     |          |                 |                 |          |        |
|                              |      |         |                           |                |       | thebill.   | Fig.6showsthereinforcingcycle. |     |          |                 | Theguardagainst |          |        |
outputregularlyandforceoneselftoexplainafewchanges;an
|     |     |     |     |     |     | allfouristhesame: |     | keepahumancapableofsaying“no,”and |     |     |     |     |     |
| --- | --- | --- | --- | --- | --- | ----------------- | --- | --------------------------------- | --- | --- | --- | --- | --- |
inabilitytoexplainisamapneedinganupdate.
installacheckthehumandoesnothavetobeawaketorun.
| Cognitivesurrender. |     | Whenthelooprunsitselfitistempting |     |     |     |     |     |     |     |     |     |     |     |
| ------------------- | --- | --------------------------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
tostophavinganopinionandjusttakewhateverithandsback.
IX. StaytheEngineer,NotJusttheOneWhoPressesGo
| Thisistheattitudeversionofthefirsttwo: |     |     |     | not“notime”but“no |     |     |     |     |     |     |     |     |     |
| -------------------------------------- | --- | --- | --- | ----------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
different
longerwanttobother.” Themorereliabletheloop,theeasierit The same loop, built by two people, can end in
oppositeplaces—andthedifferenceisnotintheloop.AsOsmani
| istooutsourcejudgment.     |     |     | Theguardisoneline—theloopcan |     |     |             |        |     |       |          |          |              |     |
| -------------------------- | --- | --- | ---------------------------- | --- | --- | ----------- | ------ | --- | ----- | -------- | -------- | ------------ | --- |
|                            |     |     |                              |     |     | writes, two | people | can | build | the same | loop and | get opposite |     |
| execute,butitcannotdecide. |     |     | Onemustatleastremaincapable  |     |     |             |        |     |       |          |          |              |     |
ofsaying“thisiswrong.” outcomes. One uses a loop to move faster on things already
Tokenblowout. Theonlycostthathitsthebilldirectly,and mastered: theyreadthecode,holdafirmsenseofdirection,and
|                  |     |          |          |                  |          | the loop | scales | judgment | they | already had. | Another | uses | the |
| ---------------- | --- | -------- | -------- | ---------------- | -------- | -------- | ------ | -------- | ---- | ------------ | ------- | ---- | --- |
| hard to estimate | in  | advance: | the loop | hatches helpers, | retries, |          |        |          |      |              |         |      |     |
andrunsroundafterround, soonebugcanspinidleallnight sameloopsotheyneverhavetounderstandagain. Sixmonths
|                                                |     |     |     |          |     | later, one | has | gotten | stronger | and the | other has | become | the |
| ---------------------------------------------- | --- | --- | --- | -------- | --- | ---------- | --- | ------ | -------- | ------- | --------- | ------ | --- |
| andproduceanunfamiliarbillratherthanfixedcode. |     |     |     | Theguard |     |            |     |        |          |         |           |        |     |
ishardcapssetbeforeshipping—per-runbudget,dailybudget, gatekeeperofamachinetheycannotread.
maxretries—soanidlebugcannotburnanentirenight’squota. Aloopisnotatoolwhosequalityisfixedbythetool. Itisso
The four share one trait: silence while the loop runs. The strongthatitamplifies,unchanged,whateveronebrings: bring
mostfascinatingthingaboutloopengineeringisthatitletsone understandinganditamplifiesunderstanding;bringlazinessand
|     |     |     |     |     |     | itamplifieslaziness. |     | Itisafaithfulmultiplicationsign,andwhat |     |     |     |     |     |
| --- | --- | --- | --- | --- | --- | -------------------- | --- | --------------------------------------- | --- | --- | --- | --- | --- |
persondoateam’swork;themostdangerousthingisthesame
spot,becauseateamargueswithitselfandonepersonplusapile itmultipliesistheperson.
ofloopseasilybecomesanechochamberwherenooneargues. The loop makes generation extremely cheap—code, plans,
7

2026WorkingNoteonAgenticSoftwareEngineeringPractice
PRs,fixes,almostfree. Whatstaysscarceisjudgment: knowing becausetheloopexecutestheirgooddecisionsahundredfold.
whichplanisright,whichlineshouldbestopped,whichoutput Thesametoolwidensthegapbetweenthetwokindsofengineer.
runs fine but is wrong at the root. The loop can generate a It does not lift everyone equally; it multiplies whatever each
| hundredoptionsbutcannottrulychoose; |     |     | orrather, | itchooses | personbrings. |     |     |     |
| ----------------------------------- | --- | --- | --------- | --------- | ------------- | --- | --- | --- |
on“looksreasonable,”not“actuallyright,”andthegapbetween
C. TheAmplifierCutsBothWays
| those two | is the reason | an engineer | exists. Loop | engineering |         |             |                           |            |
| --------- | ------------- | ----------- | ------------ | ----------- | ------- | ----------- | ------------------------- | ---------- |
|           |               |             |              |             | Because | the loop is | an amplifier of judgment, | a lapse in |
thereforedoesnotdevaluejudgment;itstripsawayeverything
thatdoesnotrequirejudgmentandleavesjudgmentasallthat judgmentisalsoamplified. Intheoldworldabaddecisioncost
remains. onehand-writtenstretchofwrongcode,limitedinblastradius
Theloopexecutesthelogicgiventoit,butdoesnotunder- andslowenoughtocatch. Inthenewworldabaddecisionis
standwhyonewantedtobuildit,whatoneactuallywants,or executedfaithfully,inbulk,ahundredtimes,byamachinethat
|                                       |     |     |                  |     | willnotpausetoaskwhetheritisright. |     | Theloopremovesthe |     |
| ------------------------------------- | --- | --- | ---------------- | --- | ---------------------------------- | --- | ----------------- | --- |
| whichspotsonewouldratherwatchoneself. |     |     | Thoseboundaries— |     |                                    |     |                   |     |
whereamanualcheckpointsits,whereitrunsonitsown—cannot slow gear that used to bail engineers out. One can no longer
bereadoutoftheloop;theyliveonlyinthebuilder’sheadand countontheprocessbeingslowenoughtonoticeamistakemid-
mustbewritteninonebyone. Themindsetonedesignswith flight,becausetheprocesshasnoslowgearleft. Thisraisesthe
istheshapetheloopgrowsinto. Twoloopsbuiltwitha“free stakesontheonethingtheloopcannotdo,anditisthereason
thedisciplineofstayingtheengineerisnotoptionalsentiment
| myself fast” | mindset | and an “I still | mean to be | the engineer” |     |     |     |     |
| ------------ | ------- | --------------- | ---------- | ------------- | --- | --- | --- | --- |
mindsetmaybeninetypercentidenticalincode;thedifference butoperationalnecessity.
isoneortwocheckpoints,andthosedecidewhether,sixmonths
OperationalDiscipline
XI.
| out,onestandsontopofthelooporishollowedoutbyit. |     |     |     | Os- |     |     |     |     |
| ----------------------------------------------- | --- | --- | --- | --- | --- | --- | --- | --- |
mani’sclosinglineistheonetokeep: buildtheloop,butbuild Ifjudgmentisthescarceresource,thepracticalquestionis
it like someone who intends to stay the engineer, not just the howtospenditwell. Threedisciplines,drawnfromthecases
personwhopressesgo. andcostsabove,areworthstatingasstandingpractice.
TheEconomicsofJudgment
|     | X.  |     |     |     | A. ReadaSample,Always |     |     |     |
| --- | --- | --- | --- | --- | --------------------- | --- | --- | --- |
Theprevioussectionmadeaclaimworthexaminingonits Thedefenseagainstcomprehensionrotisnottoreadevery-
thingtheloopproduces—thatwoulddefeatthepurpose—but
ownterms:thatloopsmakegenerationcheapandleavejudgment
scarce. Thisisnotaslogan;itisaneconomicobservationwith toreadarepresentativesample,everyday,andtoforceoneself
consequencesforhowateamshouldorganize. toexplaineachsampledchange: whatitdidandwhyitdidit
thatway. Aninabilitytoexplainachangeisaprecisesignalthat
A. WhatBecomesAbundant
one’smentalmaphasfallenbehindthecodebase,anditisfar
When a resource becomes abundant, its price falls and the cheapertodiscoverthisfromasampledPRonaquietmorning
activities organized around it reorganize. Loops make code, thanfromaproductionincidentonabadone. Thesampleneed
plans,fixes,andpullrequestsabundant—asingleengineerwith notbelarge;itneedstoberegularandgenuinelyexamined.
| awell-builtloopcanproducetheoutputofasmallteam. |     |     |     | The |     |     |     |     |
| ----------------------------------------------- | --- | --- | --- | --- | --- | --- | --- | --- |
B. CapBeforeYouShip
activitiesthatusedtoconsumeanengineer’sday,thetypingand
theboilerplateandthemechanicalrefactor,collapsetowardzero The defense against token blowout is to set hard ceilings
cost. Areasonablefirstreactionisthatthismakesengineersless beforethelooprunsunattendedforthefirsttime,notafterthe
valuable. Theoppositeistrue,butonlyforengineerswhohold first surprising bill. A per-run budget, a daily budget, and a
ontothescarcething. maximumretrycounttogetherensurethatasinglebugspinning
|     |     |     |     |     | idleovernightcannotconsumeanentirequota. |     |     | Thesenumbers |
| --- | --- | --- | --- | --- | ---------------------------------------- | --- | --- | ------------ |
B. WhatStaysScarce
arenotprimarilyaboutsavingmoney;theyarecircuitbreakers
| The scarce | resource | is the judgment | that decides | which of |              |               |                     |             |
| ---------- | -------- | --------------- | ------------ | -------- | ------------ | ------------- | ------------------- | ----------- |
|            |          |                 |              |          | that convert | an open-ended | risk into a bounded | one. A loop |
theabundantoutputstokeep. Aloopcangenerateahundred withoutcapsisaloopthathasdelegateditsspendingauthority
| candidateimplementations;itcannottellyouwhichoneisright, |     |     |     |     | toitsownbugs. |     |     |     |
| -------------------------------------------------------- | --- | --- | --- | --- | ------------- | --- | --- | --- |
onlywhichonelooksreasonable,andthegapbetween“looks
C. KeepOneDoorOpen
| reasonable” | and “is | right” is exactly | where engineering | lives. |     |     |     |     |
| ----------- | ------- | ----------------- | ----------------- | ------ | --- | --- | --- | --- |
Asgenerationapproachesfree,theentirevalueoftheengineer The defense against cognitive surrender is structural, not
concentratesintothisgap. Theworkthatremainsispurejudg- merelyattitudinal. Buildatleastonecheckpointintotheloop
ment,distilledandundilutedbythemechanicallaborthatused whereitpausesforahuman—notbecausethehumanwillalways
tosurroundit. intervene,butbecausetheexistenceofthepausekeepsthehuman
Thishasanuncomfortableimplication. Anengineerwhose inthepositionofbeingableto. Theengineerwhoweldsevery
valuewasmostlyinthemechanicallabor—fasttyping, broad doorshut,bankingonneverneedingtogoin,discoversonthe
memorizationofAPIs,willingnesstogrindthroughboilerplate— daytheymustthattheynolongerholdthekey. Theengineer
findsthatvalueevaporating,becausetheloopdoesallofitfor wholeavesonedooropencanwalkinanytimetoseewhatthe
free.Anengineerwhosevaluewasinjudgmentfindsitamplified, loop is doing. The two loops differ by a single checkpoint in
8

2026WorkingNoteonAgenticSoftwareEngineeringPractice
code; they differ enormously in who is in control six months TABLEVI
First-LoopChecklist
later.
|     |     |     |     |     |     |     | Element |     | Askyourself |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | ------- | --- | ----------- | --- | --- | --- |
BuildYourFirstLoopToday
XII.
Whatdoesitreadonatimer?(CI/
Discovery
| Stripe’spipelineistheendpoint,notthestartingpoint. |     |     |     |     | Afirst |     |        |     |                       |     |     |     |
| -------------------------------------------------- | --- | --- | --- | --- | ------ | --- | ------ | --- | --------------------- | --- | --- | --- |
|                                                    |     |     |     |     |        |     | source |     | issues/commits/inbox) |     |     |     |
loopshouldbesosmallitbarelylookslikeasystem—alittle
|                                   |     |     |     |     |     |     | Statefile |     | Which        | disk | file holds the | cross- |
| --------------------------------- | --- | --- | --- | --- | --- | --- | --------- | --- | ------------ | ---- | -------------- | ------ |
| thingthatcheckssomethingonatimer. |     |     |     |     |     |     |           |     | roundmemory? |      |                |        |
Stepone: runa/loop. AvailableafterClaudeCodev2.1.72, Evaluator Isthereanindependentcheckthat
it reruns the same task on an interval. It is session-scoped, cansay“no”?
recurringtasksexpireaftersevendays,anditrunsonthelocal Isolation Doeseachparallelagentgetitsown
| machine;turnthemachineoffanditstops. |     |     |     |     |     |     |          |     | worktree? |         |            |          |
| ------------------------------------ | --- | --- | --- | --- | --- | --- | -------- | --- | --------- | ------- | ---------- | -------- |
|                                      |     |     |     |     |     |     | Tokencap |     | Did       | you set | a spending | ceiling? |
Whostopsitifitrunsoff?
| /loop 5m    | check the deploy | # fixed: | every           | 5 min  |     |     |             |     |                              |      |              |         |
| ----------- | ---------------- | -------- | --------------- | ------ | --- | --- | ----------- | --- | ---------------------------- | ---- | ------------ | ------- |
| /loop check | the deploy       | # agent  | paces           | itself |     |     |             |     |                              |      |              |         |
|             |                  |          |                 |        |     |     | Humanreview |     | Whichsteppausesforyoutolook, |      |              |         |
| /loop       |                  | # runs   | .claude/loop.md |        |     |     |             |     |                              |      |              |         |
|             |                  |          |                 |        |     |     |             |     | rather                       | than | auto-ing all | the way |
through?
| Step two:                                | read CI and                          | issues; | triage | first. | Rerunning one |     |     |     |     |     |     |     |
| ---------------------------------------- | ------------------------------------ | ------- | ------ | ------ | ------------- | --- | --- | --- | --- | --- | --- | --- |
| lineisnotaloop.                          | Giveitaprompttolookatthreethingseach |         |        |        |               |     |     |     |     |     |     |     |
| morningandhaveitlistwhatisworthhandling. |                                      |         |        |        | Scheduledplus |     |     |     |     |     |     |     |
theloopcanrun;thelastfourdecidewhetheritgetsintotrouble
| auto-discoveryisloopentrylevel. |     |     | Thediscoverylogicshould |     |     |     |     |     |     |     |     |     |
| ------------------------------- | --- | --- | ----------------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
liveinaskill,nottheschedule. onceitdoes. Beginnersmostoftenshipwithonlythefirsttwo
built,andtheresultisaloopnobodywatchesandnobodycan
|     |     |     |     |     |     | stop,noddingatitself. |     |     | Theclosingadviceissimple: |     |     | afirstloop |
| --- | --- | --- | --- | --- | --- | --------------------- | --- | --- | ------------------------- | --- | --- | ---------- |
# .claude/skills/morning-triage/SKILL.md
NAME: morning-triage
isbettersmall,butwiththe“no”-sayingcheckandthehuman
| WHEN: invoked | each morning | by automation. |     |     |     |     |     |     |     |     |     |     |
| ------------- | ------------ | -------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
reviewpointfullyinstalled.
READ:
- CI runs that failed since yesterday A. ACompleteFirstLoop,Annotated
| - issues | opened in the | last 24h |     |     |     |     |     |     |     |     |     |     |
| -------- | ------------- | -------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
- commits merged since the last run Tomakethechecklistconcrete,thefollowingisaminimalbut
|            |               |          |        |     |     | completeloopthatinstallsallsixelements. |     |     |     |     | Itissmallenough |     |
| ---------- | ------------- | -------- | ------ | --- | --- | --------------------------------------- | --- | --- | --- | --- | --------------- | --- |
| JUDGE: for | each item, is | it worth | acting | on? |     |                                         |     |     |     |     |                 |     |
Skip noise. Keep only actionable findings. toreadinonesittingandcontainseveryorganarealloopneeds,
onlyscaleddown.
| OUTPUT: write     | findings +     | status to |           |     |     |      |                                |     |                |     |     |     |
| ----------------- | -------------- | --------- | --------- | --- | --- | ---- | ------------------------------ | --- | -------------- | --- | --- | --- |
| ./state/triage.md | (one           | row per   | finding). |     |     |      |                                |     |                |     |     |     |
|                   |                |           |           |     |     | # 1. | SCHEDULING                     | --  | a real trigger |     |     |     |
| Stepthree:        | addastatefile. |           |           |     |     | #    | (.github/workflows/triage.yml) |     |                |     |     |     |
Donotleaveresultsinthechat
on:
| window. | Writeeveryfinding,andhowfarithasbeenhandled, |     |     |     |     | schedule: |     |     |     |     |     |     |
| ------- | -------------------------------------------- | --- | --- | --- | --- | --------- | --- | --- | --- | --- | --- | --- |
intoamarkdownfile(oraLinearboard). Theagentforgets;the - cron: ’0 6 * * *’ # 06:00 daily, cloud
| repodoesnot.        |      |        |         |     |     | # 2. | DISCOVERY | -- a    | skill, not     | a wall | of text |     |
| ------------------- | ---- | ------ | ------- | --- | --- | ---- | --------- | ------- | -------------- | ------ | ------- | --- |
|                     |      |        |         |     |     | #    | invoked   | by the  | workflow:      |        |         |     |
|                     |      |        |         |     |     | run: | claude    | --skill | morning-triage |        |         |     |
| # ./state/triage.md | (the | loop’s | memory) |     |     |      |           |         |                |        |         |     |
| finding | source | status | # 3. PERSISTENCE -- state on disk
|----------------|----------|----------|
|             |                 |          |     |     |     | #   | the skill | writes | ./state/triage.md |     |     |     |
| ----------- | --------------- | -------- | --- | --- | --- | --- | --------- | ------ | ----------------- | --- | --- | --- |
| | auth test | flaky| CI #4821 | | fixing | |   |     |     |     |           |        |                   |     |     |     |
| null deref | issue 92 | PR open | # and commits it back to the repo
| | stale dep | | commit | a3| inbox | |   |     |     |      |         |            |                     |             |     |     |
| ----------- | -------- | --------- | --- | --- | --- | ---- | ------- | ---------- | ------------------- | ----------- | --- | --- |
|             |          |           |     |     |     | # 4. | HANDOFF | -- one     | worktree            | per finding |     |     |
|             |          |           |     |     |     | for  | finding | in $(parse | ./state/triage.md); |             | do  |     |
Stepfour: addanevaluator. Themostcriticalstepandthe claude --worktree "fix/$finding" \
easiesttoskip. ClaudeCode’s/goal(afterv2.1.139)runsuntil --goal "tests pass and lint is clean" \
|     |     |     |     |     |     |     | "draft | a fix | for $finding" |     |     |     |
| --- | --- | --- | --- | --- | --- | --- | ------ | ----- | ------------- | --- | --- | --- |
different
| a condition | is met, with | a   |     | model judging | whether it | done |     |     |     |     |     |     |
| ----------- | ------------ | --- | --- | ------------- | ---------- | ---- | --- | --- | --- | --- | --- | --- |
holds.
|           |                             |      |         |               |     | # 5. | VERIFICATION | --         | a fresh            | model judges |       |     |
| --------- | --------------------------- | ---- | ------- | ------------- | --- | ---- | ------------ | ---------- | ------------------ | ------------ | ----- | --- |
|           |                             |      |         |               |     | #    | /goal’s      | stop check | runs               | after each   | turn; |     |
|           |                             |      |         |               |     | #    | a second     | reviewer   | agent              | picks holes  |       |     |
| /goal all | tests in test/auth          | pass | and the | lint          |     |      |              |            |                    |              |       |     |
| step      | is clean                    |      |         |               |     |      |              |            |                    |              |       |     |
|           |                             |      |         |               |     | # 6. | HUMAN        | REVIEW --  | the open           | door         |       |     |
|           |                             |      |         |               |     | #    | PRs are      | opened,    | never auto-merged; |              |       |     |
| Stepfive: | addworktreesforparallelism. |      |         | Use--worktree |     |      |              |            |                    |              |       |     |
|           |                             |      |         |               |     | #    | anything     | uncertain  | lands              | in ./inbox/  |       |     |
(or-w)toopenanindependentworktreeperbackgroundagent
|     |     |     |     |     |     | Read | top | to bottom, | the | six numbered | comments | are the six |
| --- | --- | --- | --- | --- | --- | ---- | --- | ---------- | --- | ------------ | -------- | ----------- |
sotheydonotsteponeachother.
|     |     |     |     |     |     | elements |     | of the checklist, |     | each realized | in two | or three lines. |
| --- | --- | --- | --- | --- | --- | -------- | --- | ----------------- | --- | ------------- | ------ | --------------- |
# one isolated worktree per finding Thecronlineisscheduling;theskillinvocationisdiscovery;the
| claude --worktree | fix/auth-test |     | "draft | the fix" |     |     |     |     |     |     |     |     |
| ----------------- | ------------- | --- | ------ | -------- | --- | --- | --- | --- | --- | --- | --- | --- |
claude --worktree fix/null-deref "draft the fix" committedstatefileispersistence;theper-findingworktreeis
handoff;the/goalstopcheckplusreviewerisverification;and
OfthesixelementsinTableVI,thefirsttwodecidewhether the never-auto-merge rule with an inbox is the human review
9

2026WorkingNoteonAgenticSoftwareEngineeringPractice
point. A loop with all six, even a tiny one, is a real loop. A TABLEVII
TermsUsedinThisNote
loopmissinganyofthemisoneofthefivefailuresofSectionVI
| wearingadisguise. |     |     |     |     |     | Term |     | Meaning               |     |     |                 |     |
| ----------------- | --- | --- | --- | --- | --- | ---- | --- | --------------------- | --- | --- | --------------- | --- |
|                   |     |     |     |     |     | Loop |     | Asystemthatdiscovers, |     |     | does, verifies, |     |
B. GrowingtheLoopSafely
persists,andreschedulesworkwithouta
humanintheinnercycle.
| Oncetheminimalloopruns, |     |     |     | thetemptationistoscaleit— |     |         |     |                                    |     |     |     |     |
| ----------------------- | --- | --- | --- | ------------------------- | --- | ------- | --- | ---------------------------------- | --- | --- | --- | --- |
|                         |     |     |     |                           |     | Harness |     | Thekitarmingasingleagentrun:tools, |     |     |     |     |
morefindings,moreparallelagents,shorterintervals. Thesafe allowedactions,recovery,“done.”
orderofgrowthistoaddparallelismlast,afterthechecksare
|     |     |     |     |     |     | Move |     | Oneofthefivestepsinasingleturnofa |     |     |     |     |
| --- | --- | --- | --- | --- | --- | ---- | --- | --------------------------------- | --- | --- | --- | --- |
proven. Increasewhattheloopdiscoversbeforeincreasinghow loop.
muchitdoesinparallel,andprovetheevaluatorcatchesrealmis- Part Oneofthesixcomponentsthatrealize
takesbeforetrustingittogatemanyagentsatonce. TheStripe themoves.
|             |          |     |      |                      |                 | Generator |     | Theagentthatwrites. |     |     |     |     |
| ----------- | -------- | --- | ---- | -------------------- | --------------- | --------- | --- | ------------------- | --- | --- | --- | --- |
| case is the | endpoint | of  | this | path, not the entry: | its reliability |           |     |                     |     |     |     |     |
comesfromyearsofhardeningthedeterministicgates,notfrom Evaluator Aseparateagentthatjudges,defaulting
startinglarge. Aloopearnstherighttorunmoreagentsbyfirst todoubtandactingtoverify.
|     |     |     |     |     |     | Worktree |     | A git | mechanism | giving | each parallel |     |
| --- | --- | --- | --- | --- | --- | -------- | --- | ----- | --------- | ------ | ------------- | --- |
demonstratingitcanstopasinglebadone.
agentitsownworkingdirectory.
Therestisnotinthisnote;itisintheterminal.
|     |     |     |     |     |     | Skill |     | Projectknowledgemadepermanentina |     |     |     |     |
| --- | --- | --- | --- | --- | --- | ----- | --- | -------------------------------- | --- | --- | --- | --- |
SKILL.mdfile.
|       |                                  |     |     |     |     | Connector |     | AnMCPinterfacelinkingthelooptoex- |     |     |     |     |
| ----- | -------------------------------- | --- | --- | --- | --- | --------- | --- | --------------------------------- | --- | --- | --- | --- |
| XIII. | AppendixA:AnAnnotatedTriageSkill |     |     |     |     |           |     |                                   |     |     |     |     |
ternalsystems.
Thediscoverymovedependsonaskillratherthanawallof Memory Persistentstateondisk,survivinganysin-
gleconversation.
instructions,becauseaskillcanbereusedandmaintainedwhile
|          |        |      |               |        |                   | Intentdebt |     | The | recurring | cost of | re-explaining | a   |
| -------- | ------ | ---- | ------------- | ------ | ----------------- | ---------- | --- | --- | --------- | ------- | ------------- | --- |
| a pasted | prompt | rots | in a schedule | nobody | updates. The fol- |            |     |     |           |         |               |     |
project,paidoffbyaskill.
lowingisafullerversionofthemorning-triageskillreferenced
|     |     |     |     |     |     | Verification |     | Unverifiedoutputaccumulatingbetween |     |     |     |     |
| --- | --- | --- | --- | --- | --- | ------------ | --- | ----------------------------------- | --- | --- | --- | --- |
throughout,annotatedtoshowhoweachsectionservesamove.
|     |     |     |     |     |     | debt |     | “runs”and“right.” |     |     |     |     |
| --- | --- | --- | --- | --- | --- | ---- | --- | ----------------- | --- | --- | --- | --- |
# .claude/skills/morning-triage/SKILL.md
---
| name: morning-triage |     |     |     |     |     |     |     | AppendixB:Glossary |     |     |     |     |
| -------------------- | --- | --- | --- | --- | --- | --- | --- | ------------------ | --- | --- | --- | --- |
XIV.
| trigger: | invoked | by daily | automation |     |     |     |     |     |     |     |     |     |
| -------- | ------- | -------- | ---------- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
---
|              |           |         |     |     |     | XV. | Synthesis: | WhatthePlaybookComesDownTo |     |     |     |     |
| ------------ | --------- | ------- | --- | --- | --- | --- | ---------- | -------------------------- | --- | --- | --- | --- |
| ## Read (the | DISCOVERY | inputs) |     |     |     |     |            |                            |     |     |     |     |
- CI runs failed since the last run Acrossninesectionstheargumenthasonespine. Loopengi-
- issues opened in the last 24 hours neeringisthefourthlayerofastackthatbeganwiththeprompt
| - commits | merged | since yesterday |     |     |     |     |     |     |     |     |     |     |
| --------- | ------ | --------------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
- the previous ./state/triage.md and climbed through context and the harness, and what dis-
|          |            |         |          |          |     | tinguishes | it from      | the three | below     | is that | it removes | the hu-   |
| -------- | ---------- | ------- | -------- | -------- | --- | ---------- | ------------ | --------- | --------- | ------- | ---------- | --------- |
| ## Judge | (the part  | that    | sets the | ceiling) |     |            |              |           |           |         |            |           |
|          |            |         |          |          |     | man from   | the position | of        | doing the | work.   | A single   | turn of a |
| For each | candidate, | decide: |          |          |     |            |              |           |           |         |            |           |
- is it actionable now, or noise? loopisfivemoves—discovery,handoff,verification,persistence,
| - does | it block | a release? | →   | priority |     |     |     |     |     |     |     |     |
| ------ | -------- | ---------- | --- | -------- | --- | --- | --- | --- | --- | --- | --- | --- |
scheduling—realizedbysixparts,andthefailuresofaloopare
| - is it   | already | tracked? | → skip     |        |     |                          |     |     |                               |     |     |     |
| --------- | ------- | -------- | ---------- | ------ | --- | ------------------------ | --- | --- | ----------------------------- | --- | --- | --- |
| Keep only | what is | worth    | a worktree | today. |     |                          |     |     |                               |     |     |     |
|           |         |          |            |        |     | simplythosemovesskipped. |     |     | Thehardestofthemovesisverifi- |     |     |     |
## Write (the PERSISTENCE output) cation,becauseanagentgradingitsownworkpraisesit,andthe
Append to ./state/triage.md: reliableremedyisstructural: aseparateevaluatorthatdefaultsto
| | finding | | source | | priority |     | | status | |     |     |     |     |     |     |     |     |
| --------- | -------- | ---------- | --- | ---------- | --- | --- | --- | --- | --- | --- | --- | --- |
Commit the file so tomorrow can read it. doubt,actsratherthanreads,andisjudgedbyafreshmodelon
|             |          |     |          |     |     | anexplicitstopcondition. |     |     | Loopsalreadyruninpractice,from |     |     |     |
| ----------- | -------- | --- | -------- | --- | --- | ------------------------ | --- | --- | ------------------------------ | --- | --- | --- |
| ## Hand off | (prepare | the | HANDOFF) |     |     |                          |     |     |                                |     |     |     |
For each kept finding, emit a task line: oneengineer’smorningtoanenterprisemergingoverathousand
worktree=fix/<slug> goal=<stop-condition> machine-writtenpullrequestsaweek,andwhatmakesthemreli-
ableisthequalityoftheirconstraints,notthesizeoftheirmodel.
| ## Stop (the | boundary | you     | keep     | for yourself) |     |     |     |     |     |     |     |     |
| ------------ | -------- | ------- | -------- | ------------- | --- | --- | --- | --- | --- | --- | --- | --- |
| Never merge. | Never    | delete. | Anything | you are       |     |     |     |     |     |     |     |     |
Theyrunupfoursilentdebts—verificationdebt,comprehension
| less than | confident | about | goes | to ./inbox/ |     |     |     |     |     |     |     |     |
| --------- | --------- | ----- | ---- | ----------- | --- | --- | --- | --- | --- | --- | --- | --- |
for a human, not into a PR. rot,cognitivesurrender,andtokenblowout—thatreinforceone
|     |     |     |     |     |     | anotherandcomedueallatonce. |     |     |     | Andbecausetheloopisan |     |     |
| --- | --- | --- | --- | --- | --- | --------------------------- | --- | --- | --- | --------------------- | --- | --- |
Fiveoftheskill’ssixheadingsmaptothefivemoves;thesixth, amplifierofwhateverthebuilderbrings,thesameloopbuiltby
“Stop,” is where the builder writes in the boundary the loop twopeopleyieldsoppositeoutcomes,separatedbyoneortwo
cannotinfer. Theloopwillfaithfullydoeverythingtheskillsays checkpointsthatdecidewhoisincontrollater.
andnothingitomits,sothe“Stop”sectionisnotboilerplate—it The single sentence to carry away is the one the field con-
isthesingleplacewheretheengineer’sintentaboutwhereto vergedoninaweek: stoppromptingtheagent,anddesignthe
keepcontrolismadepermanent. Leaveitoutandtheloopwill systemthatpromptsit—butdesignitlikesomeonewhointends
mergewithconfidenceithasnotearned. tostaytheengineer,notjusttheonewhopressesgo. Everything
10

2026WorkingNoteonAgenticSoftwareEngineeringPractice
technicalinthisnoteservesthatoneposture. Theevaluator,the Acknowledgment. Thisisanindependentconference-stylesynthesisbuilton
statefile,thebudgetcap,theopendoor:eachisawayofkeeping theframeworkofHuaShu’sopenguideLoopEngineering: StopAskingMe
WhatItIs(OrangeBooks,v260615,June2026). Theframeworkandquoted
ahumancapableofsaying“no”toamachinebuilttosay“yes”
formulationsareduetoAddyOsmani;thegenerator/evaluatorfindingstoPrithvi
atspeed. Aloopisthemostpowerfultoolinthisgenerationof
Rajasekaran(Anthropic);andtheenterprisecasetoSteveKaliski(Stripe).All
softwarepracticepreciselybecauseitisthemostfaithfulmulti- productdetailsmaychange;refertoeachtool’sofficialdocumentation.
plierofitsbuilder,andafaithfulmultiplierisexactlyasvaluable,
orasdangerous,asthejudgmentfedintoit.
A. FieldNotesfortheFirstMonth
Forateamadoptingloops,afewpracticalobservationsrecur
oftenenoughtobeworthstatingplainly. First,theloopthatsur-
vivesisthesmallonethatearnedtrust,nottheambitiousonethat
demandedit;startwithasinglefindinghandledendtoendand
widenonlyafterthecheckshavecaughtrealmistakes. Second,
theevaluatoriswheretheengineeringeffortbelongs—astrong
generatorwithaweakjudgeproducesconfidentgarbage,while
amodestgeneratorwithasharpjudgeproducesslow,reliable
progress,andthesecondiswhatcompounds. Third,thehuman
reviewpointisnotatemporaryscaffoldtoberemovedoncethe
loopistrusted; itisthepermanentfeaturethatkeepstheloop
trustworthy, and the day it is removed is the day comprehen-
sionrotbeginsinearnest. Fourth,budgetcapsshouldbeseton
theassumptionthatsomethingwillspinidleovernight,because
eventuallysomethingwill,andthecapisthedifferencebetween
acuriosityinthelogsandalineitemonaninvoice.
Noneoftheseissurprisinginisolation. Whatsurprisesteams
ishowquicklythepleasantexperienceofaloopthat“justworks”
erodesthedisciplinesthatmadeitwork,andhowtheerosionis
invisibleuntilthemorningitisnot. Theplaybook,intheend,is
lessaboutbuildingloops—thatpartisgenuinelyeasynow—and
moreaboutremainingthekindofengineerwhocanstillanswer,
onanygivenmorning,whetherthethingtheloopjustdidwas
actuallyright.
References
[1] A.Osmani,“LoopEngineering,”personalblogandSubstack,Jun.2026.
[2] P.Steinberger,postondesigningloopsthatpromptcodingagents,social
media,Jun.2026.
[3] B.Cherny,publicremarksonwritingloopsthatpromptClaude,Anthropic,
Jun.2026.
[4] P.Rajasekaran,“Buildinglong-runningagenticapplications:thegenera-
tor/evaluatorpattern,”Anthropicengineeringblog,2026.
[5] S.Kaliski,“Stripe’sMinions:1,300PRsaweek,”HowIAIpodcast,2026.
[6] “ModelContextProtocol(MCP)specification,”openstandard,2025–2026.
[7] “Goose:anopen-sourceagentframework,”projectdocumentation,2025–
2026.
[8] “ClaudeCodedocumentation:/loop,/goal,worktrees,skills,automa-
tions,”Anthropic,2026.
[9] HuaShu,LoopEngineering: StopAskingMeWhatItIs,OrangeBooks,
v260615,Jun.2026.
11
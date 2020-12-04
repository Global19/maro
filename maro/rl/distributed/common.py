from enum import Enum


class LearnerActorComponent(Enum):
    LEARNER = "learner"
    ACTOR = "actor"


class ActorTrainerComponent(Enum):
    ACTOR = "actor"
    TRAINER = "trainer"


class MessageTag(Enum):
    ROLLOUT = "rollout"
    EXPLORATION_PARAMS = "exploration_params"
    EXPLORATION_PARAMS_ACK = "exploration_params_ack"
    UPDATE = "update"
    CHOOSE_ACTION = "choose_action"
    ACTION = "action"
    MODEL = "model"
    EXIT = "exit"


class PayloadKey(Enum):
    ACTOR_ID = "actor_id"
    ACTION = "action"
    AGENT_ID = "agent_id"
    MODEL = "model"
    EXPLORATION_PARAMS = "exploration_params"
    PERFORMANCE = "performance"
    EXPERIENCES = "experiences"
    STATE = "state"
    SEED = "seed"
    DONE = "done"
    RETURN_DETAILS = "return_experiences"